"""Server side of the Fabricators mobile app.

The app is a WebView shell over the desk, helpdesk and CRM UIs, which only know the
sid cookie. It signs in once with Authorization Code + PKCE against the Fab Mobile
OAuth client, keeps the refresh token behind biometrics and trades a fresh access
token for a cookie session on every open.
"""

import html
import json
import os
import re

import frappe
import requests
from frappe import _
from frappe.core.doctype.activity_log.activity_log import add_authentication_log
from frappe.utils import (
	add_to_date,
	cint,
	get_fullname,
	get_url,
	get_url_to_form,
	now_datetime,
	strip_html,
)
from werkzeug.exceptions import abort
from werkzeug.wrappers import Response

# Fixed so the app can hardcode it, see fab.install.ensure_mobile_oauth_client.
MOBILE_OAUTH_CLIENT = "fab-mobile"
MOBILE_PACKAGE = "srl.fabricators.erp"
MOBILE_CALLBACK_PATH = "/mobile/callback"
MOBILE_GRANT_TYPES = ("authorization_code", "refresh_token")
# Frappe stores the field but never sets or checks it; the app swaps the code at once.
AUTHORIZATION_CODE_TTL = 120
TOKEN_ENDPOINT = "/api/method/frappe.integrations.oauth2.get_token"
REGISTER_DEVICE_LIMIT = 20

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{}/messages:send"
FCM_TOKEN_CACHE_KEY = "fab_fcm_access_token"
FCM_MISSING_CACHE_KEY = "fab_fcm_missing_logged"
PUSH_BODY_LENGTH = 180


def get_mobile_redirect_uri() -> str:
	"""An https App Link / Universal Link on the site itself: once the OS has verified
	the domain only our app can receive the code, which a custom scheme cannot promise."""
	return get_url(MOBILE_CALLBACK_PATH)


@frappe.whitelist(methods=["POST"])
def session():
	"""Open a cookie session for the user of the app's OAuth access token.

	Only a live bearer of the Fab Mobile client qualifies: an API key, another client's
	token or a cookie session must not be able to mint a new sid. A cross-site page
	cannot set the Authorization header without a CORS preflight, which Frappe does
	not grant, so the endpoint needs no CSRF token of its own.
	"""
	token = get_mobile_bearer_token()
	reject_impersonation()
	if frappe.db.get_value("User", token.user, "user_type") != "System User":
		frappe.throw(_("The mobile app is for system users only."), frappe.AuthenticationError)
	frappe.local.login_manager.login_as(token.user)
	add_authentication_log(
		_("{0} opened a session from the mobile app").format(get_fullname(token.user)), token.user
	)


@frappe.whitelist(methods=["POST"])
def logout(refresh_token: str | None = None, push_token: str | None = None):
	"""Revoke the app's tokens, forget the device and close the cookie session.

	Called from the WebView it closes the sid as well; called with the bearer only the
	tokens and the device go, since that request carries no session to close.
	"""
	user = frappe.session.user
	if push_token:
		frappe.db.delete("Mobile Device", {"push_token": push_token, "user": user})
	if refresh_token:
		frappe.db.set_value(
			"OAuth Bearer Token",
			{"refresh_token": refresh_token, "user": user, "client": MOBILE_OAUTH_CLIENT},
			"status",
			"Revoked",
		)

	access_token = get_bearer_header()
	if access_token:
		frappe.db.set_value(
			"OAuth Bearer Token",
			{"name": access_token, "user": user, "client": MOBILE_OAUTH_CLIENT},
			"status",
			"Revoked",
		)
	else:
		frappe.local.login_manager.logout()


@frappe.whitelist(methods=["POST"])
def register_device(
	push_token: str, platform: str, device_name: str | None = None, app_version: str | None = None
):
	"""Upsert by push token: a phone handed to a colleague keeps its token, so the
	device moves to whoever registers it last."""
	user = frappe.session.user
	reject_impersonation()
	if frappe.db.get_value("User", user, "user_type") != "System User":
		frappe.throw(_("The mobile app is for system users only."), frappe.PermissionError)

	name = frappe.db.get_value("Mobile Device", {"push_token": push_token})
	doc = frappe.get_doc("Mobile Device", name) if name else frappe.new_doc("Mobile Device")
	# the app registers on every unlock; only new or moved tokens count
	if doc.user != user:
		check_register_device_rate(user)
	doc.update(
		{
			"user": user,
			"push_token": push_token,
			"platform": platform,
			"device_name": device_name,
			"app_version": app_version,
			"last_seen": now_datetime(),
		}
	)
	doc.save(ignore_permissions=True)
	return doc.name


def check_register_device_rate(user: str):
	"""frappe.rate_limiter keys on the IP or a form field, not on the user, so the
	per-user window is kept here."""
	key = f"fab_register_device:{user}"
	if not frappe.cache.get(key):
		frappe.cache.setex(key, 3600, 0)
	if frappe.cache.incrby(key, 1) > REGISTER_DEVICE_LIMIT:
		frappe.throw(_("Too many device registrations, try again later."), frappe.RateLimitExceededError)


def reject_impersonation():
	if frappe.session.data.get("impersonated_by"):
		frappe.throw(_("Not available while impersonating a user."), frappe.PermissionError)


def get_bearer_header() -> str | None:
	scheme, _sep, value = frappe.get_request_header("Authorization", "").partition(" ")
	if scheme.lower() != "bearer":
		return None
	return value.strip() or None


def get_mobile_bearer_token():
	"""The request's bearer, read again from the header: frappe.session.user alone does
	not tell an OAuth request from an API key or a cookie one."""
	access_token = get_bearer_header()
	token = access_token and frappe.db.get_value(
		"OAuth Bearer Token",
		access_token,
		["user", "client", "status", "expiration_time"],
		as_dict=True,
	)
	if (
		not token
		or token.client != MOBILE_OAUTH_CLIENT
		or token.status != "Active"
		or token.expiration_time <= now_datetime()
		or token.user != frappe.session.user
		or not frappe.db.get_value("User", token.user, "enabled")
	):
		frappe.throw(_("A valid mobile app access token is required."), frappe.AuthenticationError)
	return token


def check_authorization_code(doc, method=None):
	"""OAuth Authorization Code before_insert.

	Frappe checks PKCE only when the code carries a challenge and never looks at the
	code's expiry, so for the app both are required here.
	"""
	if doc.client != MOBILE_OAUTH_CLIENT:
		return
	reject_impersonation()
	if not doc.code_challenge or doc.code_challenge_method != "s256":
		frappe.throw(_("The mobile app must sign in with PKCE (S256)."), frappe.PermissionError)
	doc.expiration_time = add_to_date(now_datetime(), seconds=AUTHORIZATION_CODE_TTL)


def check_bearer_token(doc, method=None):
	"""OAuth Bearer Token before_insert.

	Frappe accepts the password grant from any client, lets a refresh token of one
	client mint tokens for another, and leaves the old refresh token active after a
	refresh. The app gets the code and refresh grants only, and one live token per
	login, so a logout that revokes it ends the login.
	"""
	grant_type = frappe.form_dict.get("grant_type")
	if grant_type == "refresh_token":
		old = frappe.db.get_value(
			"OAuth Bearer Token",
			{"refresh_token": frappe.form_dict.get("refresh_token")},
			["name", "client"],
			as_dict=True,
		)
		if not old or old.client != doc.client:
			frappe.throw(_("Invalid refresh token."), frappe.AuthenticationError)

	if doc.client != MOBILE_OAUTH_CLIENT:
		return
	if grant_type not in MOBILE_GRANT_TYPES:
		frappe.throw(_("Grant type not allowed for the mobile app."), frappe.AuthenticationError)

	if grant_type == "authorization_code":
		expires = frappe.db.get_value(
			"OAuth Authorization Code", frappe.form_dict.get("code"), "expiration_time"
		)
		if not expires or expires < now_datetime():
			frappe.throw(_("The authorization code has expired."), frappe.AuthenticationError)
		return

	# a concurrent refresh with the same token waits on the row lock and then finds it
	# revoked, so only one of the two gets a new token
	frappe.db.sql(
		"update `tabOAuth Bearer Token` set status = 'Revoked' where name = %s and status = 'Active'",
		old.name,
	)
	if not frappe.db._cursor.rowcount:
		frappe.throw(_("Invalid refresh token."), frappe.AuthenticationError)


def revoke_mobile_tokens(user: str):
	frappe.db.set_value(
		"OAuth Bearer Token",
		{"user": user, "client": MOBILE_OAUTH_CLIENT, "status": "Active"},
		"status",
		"Revoked",
	)


def on_user_update(doc, method=None):
	"""User on_update: a disabled user loses the app, a new password logs the app out.
	User keeps the new password in a private attribute, cleared from the field by then."""
	if doc.has_value_changed("enabled") and not doc.enabled:
		frappe.db.delete("Mobile Device", {"user": doc.name})
		revoke_mobile_tokens(doc.name)
	elif getattr(doc, "_User__new_password", None):
		revoke_mobile_tokens(doc.name)


def before_request():
	if frappe.request.method in ("GET", "HEAD"):
		serve_mobile_paths(frappe.request.path)
	elif frappe.request.path == TOKEN_ENDPOINT:
		detect_refresh_token_reuse()


def detect_refresh_token_reuse():
	"""A revoked refresh token of the app coming back means it was copied: the thief
	and the app each hold a copy of the chain, so all of the user's app tokens go.

	Frappe refuses the revoked token itself and rolls the request back, hence the commit.
	"""
	if frappe.form_dict.get("grant_type") != "refresh_token" or not frappe.form_dict.get("refresh_token"):
		return
	old = frappe.db.get_value(
		"OAuth Bearer Token",
		{"refresh_token": frappe.form_dict.refresh_token},
		["user", "client", "status"],
		as_dict=True,
	)
	if old and old.client == MOBILE_OAUTH_CLIENT and old.status == "Revoked":
		revoke_mobile_tokens(old.user)
		add_authentication_log(
			_("Reused mobile app refresh token, all mobile app sessions revoked"), old.user, status="Failed"
		)
		frappe.db.commit()


def serve_mobile_paths(path: str):
	"""Frappe answers every GET under /.well-known/ itself before any website route or
	page renderer runs, so the app link files are served from before_request by aborting
	with the finished response. Without the site_config keys the path falls through to
	Frappe's 404."""
	if path == "/.well-known/assetlinks.json":
		fingerprints = frappe.conf.get("fab_mobile_android_sha256")
		if fingerprints:
			abort(json_response(get_assetlinks(fingerprints)))
	elif path == "/.well-known/apple-app-site-association":
		team_id = frappe.conf.get("fab_mobile_apple_team_id")
		if team_id:
			abort(json_response(get_apple_app_site_association(team_id)))
	elif path == MOBILE_CALLBACK_PATH:
		abort(callback_page())


def get_assetlinks(fingerprints: str | list[str]) -> list[dict]:
	return [
		{
			"relation": ["delegate_permission/common.handle_all_urls"],
			"target": {
				"namespace": "android_app",
				"package_name": MOBILE_PACKAGE,
				"sha256_cert_fingerprints": [fingerprints] if isinstance(fingerprints, str) else fingerprints,
			},
		}
	]


def get_apple_app_site_association(team_id: str) -> dict:
	"""Both the iOS 13+ keys and the older appID/paths ones, iOS reads whichever it knows."""
	app_id = f"{team_id}.{MOBILE_PACKAGE}"
	pattern = f"{MOBILE_CALLBACK_PATH}*"
	return {
		"applinks": {
			"apps": [],
			"details": [
				{"appIDs": [app_id], "components": [{"/": pattern}], "appID": app_id, "paths": [pattern]}
			],
		},
		# ASWebAuthenticationSession checks it before it accepts an https callback
		"webcredentials": {"apps": [app_id]},
	}


def json_response(data) -> Response:
	return Response(
		json.dumps(data), content_type="application/json", headers={"Cache-Control": "max-age=3600"}
	)


def callback_page() -> Response:
	"""Only seen when the app is missing or the link is not verified yet. The URL holds
	the authorization code, so the page loads nothing and sends no referrer."""
	title = html.escape(_("Open the Fabricators app"))
	hint = html.escape(_("If the app did not open, open it yourself to finish signing in."))
	# a tap on the same link lets Android hand it to the app when a redirect alone did not
	link = html.escape(get_url(frappe.request.full_path))
	body = (
		f'<!DOCTYPE html><html lang="{html.escape(frappe.local.lang or "en")}"><head><meta charset="utf-8">'
		'<meta name="viewport" content="width=device-width, initial-scale=1">'
		f"<title>{title}</title><style>body{{font-family:system-ui,sans-serif;margin:0;"
		"min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center;"
		f"padding:16px}}h1{{font-size:1.4rem}}p{{color:#555}}</style></head><body><main><h1>{title}</h1>"
		f'<p>{hint}</p><p><a href="{link}">{title}</a></p></main></body></html>'
	)
	return Response(
		body,
		content_type="text/html; charset=utf-8",
		headers={
			"Cache-Control": "no-store",
			"Referrer-Policy": "no-referrer",
			"X-Robots-Tag": "noindex",
			"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
		},
	)


def can_push(user: str) -> bool:
	"""Only while the user may still use the app: enabled, with a live app login."""
	return bool(
		frappe.db.get_value("User", user, "enabled")
		and frappe.db.exists(
			"OAuth Bearer Token", {"user": user, "client": MOBILE_OAUTH_CLIENT, "status": "Active"}
		)
	)


def notify_devices(doc, method=None):
	"""Notification Log after_insert: mirror the desk bell on the user's phones."""
	if not doc.for_user or not frappe.db.exists("Mobile Device", {"user": doc.for_user}):
		return
	send_push(
		doc.for_user, doc.subject, doc.email_content, get_document_url(doc.document_type, doc.document_name)
	)


def get_document_url(doctype: str | None, name: str | None) -> str | None:
	if not (doctype and name):
		return None
	if doctype == "HD Ticket":
		return get_url(f"/helpdesk/tickets/{name}")
	return get_url_to_form(doctype, name)


def send_push(user: str, title: str, body: str | None = None, url: str | None = None):
	"""Push to every device of the user once the current transaction commits, as long
	as the user is enabled and still signed in to the app.

	Title and body may carry HTML, as notification subjects do; they are reduced to
	plain text here so callers can pass them as they are.
	"""
	if not can_push(user):
		return
	if cint(frappe.conf.get("fab_push_preview", 1)):
		title, body = to_plain_text(title), to_plain_text(body, PUSH_BODY_LENGTH)
	else:
		# site_config fab_push_preview = 0: nothing of the document on the lock screen
		lang = frappe.db.get_value("User", user, "language") or frappe.db.get_default("lang")
		title, body = _("New notification", lang=lang), None
	frappe.enqueue(
		"fab.mobile.deliver_push",
		queue="short",
		enqueue_after_commit=True,
		user=user,
		title=title,
		body=body,
		url=url,
	)


def to_plain_text(value: str | None, length: int | None = None) -> str:
	value = re.sub(r"<(script|style)\b.*?</\1\s*>", "", value or "", flags=re.S | re.I)
	text = re.sub(r"\s+", " ", html.unescape(strip_html(value))).strip()
	if length and len(text) > length:
		text = text[: length - 3].rstrip() + "..."
	return text


def deliver_push(user: str, title: str, body: str | None = None, url: str | None = None):
	tokens = frappe.get_all("Mobile Device", filters={"user": user}, pluck="push_token")
	if not tokens or not can_push(user):
		return

	account = get_fcm_service_account()
	if not account:
		return

	http = requests.Session()
	http.headers["Authorization"] = f"Bearer {get_fcm_access_token(account)}"
	send_url = FCM_SEND_URL.format(account["project_id"])
	for token in tokens:
		message = {
			"token": token,
			"notification": {"title": title},
			"android": {"priority": "high"},
			"apns": {"payload": {"aps": {"sound": "default"}}},
		}
		if body:
			message["notification"]["body"] = body
		if url:
			message["data"] = {"url": url}
		response = http.post(send_url, json={"message": message}, timeout=15)
		if response.ok:
			continue
		if is_dead_token(response):
			frappe.db.delete("Mobile Device", {"push_token": token})
		else:
			frappe.log_error("FCM push failed", f"{response.status_code}: {response.text}")


def is_dead_token(response) -> bool:
	"""UNREGISTERED is an uninstalled app; INVALID_ARGUMENT counts only when FCM blames
	the token, not the payload."""
	try:
		error = response.json().get("error", {})
	except ValueError:
		return False
	for detail in error.get("details", []):
		if detail.get("errorCode") == "UNREGISTERED":
			return True
		if any(v.get("field") == "message.token" for v in detail.get("fieldViolations", [])):
			return True
	return False


def get_fcm_service_account() -> dict | None:
	"""site_config `fab_fcm_service_account`: the Firebase service account JSON inline,
	as a string or a path (relative to the site folder). Without it pushes are a no-op
	until a Firebase project exists."""
	value = frappe.conf.get("fab_fcm_service_account")
	if isinstance(value, str) and not value.lstrip().startswith("{"):
		path = value if os.path.isabs(value) else frappe.get_site_path(value)
		value = frappe.read_file(path)
	if isinstance(value, str):
		value = json.loads(value)

	if not value:
		# jobs run in forked workers, so a module flag would not hold; once a day is enough
		if not frappe.cache.get_value(FCM_MISSING_CACHE_KEY):
			frappe.logger("fab.mobile").warning(
				"fab_fcm_service_account is not set, push notifications are off"
			)
			frappe.cache.set_value(FCM_MISSING_CACHE_KEY, 1, expires_in_sec=86400)
		return None
	return value


def get_fcm_access_token(account: dict) -> str:
	if token := frappe.cache.get_value(FCM_TOKEN_CACHE_KEY):
		return token

	from google.auth.transport.requests import Request
	from google.oauth2 import service_account

	credentials = service_account.Credentials.from_service_account_info(account, scopes=[FCM_SCOPE])
	credentials.refresh(Request())
	# google-auth returns a naive UTC expiry; keep a minute of margin
	ttl = int((credentials.expiry - now_utc()).total_seconds()) - 60
	frappe.cache.set_value(FCM_TOKEN_CACHE_KEY, credentials.token, expires_in_sec=max(ttl, 60))
	return credentials.token


def now_utc():
	from datetime import UTC, datetime

	return datetime.now(UTC).replace(tzinfo=None)
