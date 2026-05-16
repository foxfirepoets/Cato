"""Integration metadata and action registry for Cato's builder tool layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class IntegrationAction:
    """One builder-facing action Cato can plan or execute."""

    name: str
    description: str
    method: str = "GET"
    path: str = ""
    base_url: str = ""
    write: bool = False
    required_params: tuple[str, ...] = ()
    optional_params: tuple[str, ...] = ()
    query_params: tuple[str, ...] = ()
    body_format: str = "json"
    auth: str = "bearer"
    approval_note: str = ""


@dataclass(frozen=True)
class IntegrationDefinition:
    """Static metadata for a supported integration."""

    integration_id: str
    display_name: str
    category: str
    credential_groups: tuple[tuple[str, ...], ...]
    base_url: str = ""
    actions: dict[str, IntegrationAction] = field(default_factory=dict)
    auth_type: str = "api_key"
    docs_url: str = ""
    setup_steps: tuple[str, ...] = ()
    oauth_authorize_url: str = ""
    oauth_scopes: tuple[str, ...] = ()
    notes: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.integration_id,
            "display_name": self.display_name,
            "category": self.category,
            "credential_options": [list(group) for group in self.credential_groups],
            "base_url": self.base_url,
            "auth_type": self.auth_type,
            "docs_url": self.docs_url,
            "setup_steps": list(self.setup_steps),
            "oauth_authorize_url": self.oauth_authorize_url,
            "oauth_scopes": list(self.oauth_scopes),
            "actions": {
                name: {
                    "description": action.description,
                    "method": action.method,
                    "base_url": action.base_url,
                    "write": action.write,
                    "required_params": list(action.required_params),
                    "optional_params": list(action.optional_params),
                    "query_params": list(action.query_params),
                    "approval_required": action.write,
                    "approval_note": action.approval_note,
                }
                for name, action in sorted(self.actions.items())
            },
            "notes": self.notes,
        }


def _action(
    name: str,
    description: str,
    method: str,
    path: str,
    *,
    base_url: str = "",
    write: bool = False,
    required_params: tuple[str, ...] = (),
    optional_params: tuple[str, ...] = (),
    query_params: tuple[str, ...] = (),
    body_format: str = "json",
    auth: str = "bearer",
    approval_note: str = "",
) -> IntegrationAction:
    return IntegrationAction(
        name=name,
        description=description,
        method=method,
        path=path,
        base_url=base_url,
        write=write,
        required_params=required_params,
        optional_params=optional_params,
        query_params=query_params,
        body_format=body_format,
        auth=auth,
        approval_note=approval_note,
    )


_INTEGRATIONS: dict[str, IntegrationDefinition] = {
    "github": IntegrationDefinition(
        integration_id="github",
        display_name="GitHub",
        category="code",
        credential_groups=(("GITHUB_TOKEN", "GH_TOKEN", "github_token"),),
        base_url="https://api.github.com",
        docs_url="https://docs.github.com/rest",
        setup_steps=(
            "Create a fine-grained GitHub token with repository scopes needed for the repos Cato may manage.",
            "Store it in Cato's vault as GITHUB_TOKEN or GH_TOKEN.",
            "Keep production repository creation and PR actions approval-gated.",
        ),
        actions={
            "list_repos": _action("list_repos", "List repositories for the authenticated user.", "GET", "/user/repos", query_params=("per_page", "page", "visibility", "affiliation")),
            "get_repo": _action("get_repo", "Get one repository.", "GET", "/repos/{owner}/{repo}", required_params=("owner", "repo")),
            "create_repo": _action("create_repo", "Create a repository for the authenticated user.", "POST", "/user/repos", write=True, required_params=("name",), optional_params=("private", "description", "auto_init"), approval_note="Creates a new GitHub repository."),
            "list_issues": _action("list_issues", "List repository issues.", "GET", "/repos/{owner}/{repo}/issues", required_params=("owner", "repo"), query_params=("state", "labels", "per_page", "page")),
            "create_issue": _action("create_issue", "Create a GitHub issue.", "POST", "/repos/{owner}/{repo}/issues", write=True, required_params=("owner", "repo", "title"), optional_params=("body", "labels", "assignees"), approval_note="Creates a user-visible issue."),
            "list_pull_requests": _action("list_pull_requests", "List pull requests.", "GET", "/repos/{owner}/{repo}/pulls", required_params=("owner", "repo"), query_params=("state", "per_page", "page")),
            "create_pull_request": _action("create_pull_request", "Create a pull request.", "POST", "/repos/{owner}/{repo}/pulls", write=True, required_params=("owner", "repo", "title", "head", "base"), optional_params=("body", "draft"), approval_note="Creates a user-visible pull request."),
        },
    ),
    "vercel": IntegrationDefinition(
        integration_id="vercel",
        display_name="Vercel",
        category="deployment",
        credential_groups=(("VERCEL_TOKEN", "vercel_token"),),
        base_url="https://api.vercel.com",
        docs_url="https://vercel.com/docs/rest-api",
        setup_steps=(
            "Create a Vercel account token.",
            "Store it in Cato's vault as VERCEL_TOKEN.",
            "Use dry-run deployment planning first; production deployment remains approval-gated.",
        ),
        actions={
            "list_projects": _action("list_projects", "List Vercel projects.", "GET", "/v9/projects", query_params=("teamId", "limit", "from")),
            "create_project": _action("create_project", "Create a Vercel project.", "POST", "/v10/projects", write=True, required_params=("name",), optional_params=("framework", "gitRepository", "buildCommand", "outputDirectory"), approval_note="Creates a Vercel project."),
            "list_deployments": _action("list_deployments", "List Vercel deployments.", "GET", "/v6/deployments", query_params=("projectId", "teamId", "limit")),
            "create_deployment": _action("create_deployment", "Create a Vercel deployment from a prepared payload.", "POST", "/v13/deployments", write=True, required_params=("name",), optional_params=("project", "target", "gitSource", "files"), approval_note="Can publish a new deployment."),
            "set_project_env": _action("set_project_env", "Set a Vercel project environment variable.", "POST", "/v10/projects/{project_id}/env", write=True, required_params=("project_id", "key", "value", "target", "type"), approval_note="Stores a secret/config value in Vercel."),
        },
    ),
    "netlify": IntegrationDefinition(
        integration_id="netlify",
        display_name="Netlify",
        category="deployment",
        credential_groups=(("NETLIFY_AUTH_TOKEN", "NETLIFY_TOKEN", "netlify_token"),),
        base_url="https://api.netlify.com",
        docs_url="https://open-api.netlify.com/",
        setup_steps=(
            "Create a Netlify personal access token.",
            "Store it in Cato's vault as NETLIFY_AUTH_TOKEN or NETLIFY_TOKEN.",
            "Use build/deploy actions only after previewing the dry-run request.",
        ),
        actions={
            "list_sites": _action("list_sites", "List Netlify sites.", "GET", "/api/v1/sites"),
            "create_site": _action("create_site", "Create a Netlify site.", "POST", "/api/v1/sites", write=True, optional_params=("name", "repo"), approval_note="Creates a Netlify site."),
            "list_deploys": _action("list_deploys", "List site deploys.", "GET", "/api/v1/sites/{site_id}/deploys", required_params=("site_id",)),
            "trigger_build": _action("trigger_build", "Trigger a Netlify build.", "POST", "/api/v1/sites/{site_id}/builds", write=True, required_params=("site_id",), approval_note="Starts a Netlify build/deploy pipeline."),
        },
    ),
    "render": IntegrationDefinition(
        integration_id="render",
        display_name="Render",
        category="deployment",
        credential_groups=(("RENDER_API_KEY", "render_api_key"),),
        base_url="https://api.render.com",
        docs_url="https://api-docs.render.com/",
        setup_steps=(
            "Create a Render API key.",
            "Store it in Cato's vault as RENDER_API_KEY.",
            "Keep deploy triggers approval-gated until the target service is confirmed.",
        ),
        actions={
            "list_services": _action("list_services", "List Render services.", "GET", "/v1/services"),
            "get_service": _action("get_service", "Get one Render service.", "GET", "/v1/services/{service_id}", required_params=("service_id",)),
            "list_deploys": _action("list_deploys", "List Render deploys for a service.", "GET", "/v1/services/{service_id}/deploys", required_params=("service_id",), query_params=("limit", "cursor")),
            "trigger_deploy": _action("trigger_deploy", "Trigger a Render deploy.", "POST", "/v1/services/{service_id}/deploys", write=True, required_params=("service_id",), optional_params=("clearCache",), approval_note="Starts a Render deployment."),
        },
    ),
    "supabase": IntegrationDefinition(
        integration_id="supabase",
        display_name="Supabase",
        category="database",
        credential_groups=(("SUPABASE_ACCESS_TOKEN", "supabase_access_token"), ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_ANON_KEY")),
        base_url="https://api.supabase.com",
        docs_url="https://supabase.com/docs/reference/api",
        setup_steps=(
            "Create a Supabase access token for management actions.",
            "Store management token as SUPABASE_ACCESS_TOKEN.",
            "Store project service/anon keys separately only for project-scoped runtime use.",
        ),
        actions={
            "list_projects": _action("list_projects", "List Supabase projects.", "GET", "/v1/projects"),
            "get_project": _action("get_project", "Get one Supabase project.", "GET", "/v1/projects/{ref}", required_params=("ref",)),
            "create_project": _action("create_project", "Create a Supabase project.", "POST", "/v1/projects", write=True, required_params=("name", "organization_id", "region", "plan"), optional_params=("db_pass",), approval_note="Creates billable cloud infrastructure."),
            "list_organizations": _action("list_organizations", "List Supabase organizations.", "GET", "/v1/organizations"),
        },
    ),
    "stripe": IntegrationDefinition(
        integration_id="stripe",
        display_name="Stripe",
        category="payments",
        credential_groups=(("STRIPE_API_KEY", "STRIPE_SECRET_KEY", "stripe_api_key"),),
        base_url="https://api.stripe.com",
        docs_url="https://docs.stripe.com/api",
        setup_steps=(
            "Start with a Stripe test-mode secret key.",
            "Store it in Cato's vault as STRIPE_SECRET_KEY or STRIPE_API_KEY.",
            "Require approval before creating live products, prices, payment links, checkout sessions, or customers.",
        ),
        actions={
            "list_products": _action("list_products", "List Stripe products.", "GET", "/v1/products", query_params=("limit", "active")),
            "create_product": _action("create_product", "Create a Stripe product.", "POST", "/v1/products", write=True, required_params=("name",), optional_params=("description", "metadata"), body_format="form", approval_note="Creates a product in Stripe."),
            "create_price": _action("create_price", "Create a Stripe price.", "POST", "/v1/prices", write=True, required_params=("currency", "unit_amount", "product"), optional_params=("recurring[interval]", "nickname"), body_format="form", approval_note="Creates a billable price."),
            "create_customer": _action("create_customer", "Create a Stripe customer.", "POST", "/v1/customers", write=True, optional_params=("email", "name", "metadata"), body_format="form", approval_note="Creates a customer record."),
            "create_payment_link": _action("create_payment_link", "Create a Stripe payment link.", "POST", "/v1/payment_links", write=True, required_params=("line_items[0][price]", "line_items[0][quantity]"), body_format="form", approval_note="Creates a public payment link."),
            "create_checkout_session": _action("create_checkout_session", "Create a Stripe Checkout Session.", "POST", "/v1/checkout/sessions", write=True, required_params=("mode", "success_url", "cancel_url", "line_items[0][price]", "line_items[0][quantity]"), body_format="form", approval_note="Creates a customer-facing checkout session."),
            "create_billing_portal_session": _action("create_billing_portal_session", "Create a Stripe billing portal session.", "POST", "/v1/billing_portal/sessions", write=True, required_params=("customer", "return_url"), body_format="form", approval_note="Creates a customer-facing billing portal session."),
        },
    ),
    "google_workspace": IntegrationDefinition(
        integration_id="google_workspace",
        display_name="Google Workspace",
        category="productivity",
        credential_groups=(("GOOGLE_WORKSPACE_ACCESS_TOKEN", "GOOGLE_ACCESS_TOKEN", "google_access_token"),),
        base_url="https://www.googleapis.com",
        auth_type="oauth2",
        docs_url="https://developers.google.com/workspace",
        oauth_authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        oauth_scopes=(
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/spreadsheets",
        ),
        setup_steps=(
            "Create a Google Cloud OAuth client for desktop/web use.",
            "Authorize Gmail, Calendar, Drive, Docs, and Sheets scopes intentionally.",
            "Store the access token as GOOGLE_WORKSPACE_ACCESS_TOKEN; refresh-token automation is a follow-up step.",
        ),
        actions={
            "gmail_list_messages": _action("gmail_list_messages", "List Gmail messages.", "GET", "/gmail/v1/users/me/messages", query_params=("q", "maxResults", "pageToken")),
            "gmail_get_message": _action("gmail_get_message", "Get a Gmail message.", "GET", "/gmail/v1/users/me/messages/{message_id}", required_params=("message_id",), query_params=("format",)),
            "calendar_list_events": _action("calendar_list_events", "List Google Calendar events.", "GET", "/calendar/v3/calendars/{calendar_id}/events", required_params=("calendar_id",), query_params=("timeMin", "timeMax", "maxResults", "singleEvents", "orderBy")),
            "calendar_create_event": _action("calendar_create_event", "Create a Google Calendar event.", "POST", "/calendar/v3/calendars/{calendar_id}/events", write=True, required_params=("calendar_id", "summary", "start", "end"), optional_params=("description", "attendees", "location"), approval_note="Creates a calendar event."),
            "drive_list_files": _action("drive_list_files", "List Google Drive files.", "GET", "/drive/v3/files", query_params=("q", "pageSize", "fields")),
            "docs_get": _action("docs_get", "Get a Google Doc.", "GET", "/v1/documents/{document_id}", base_url="https://docs.googleapis.com", required_params=("document_id",)),
            "sheets_get": _action("sheets_get", "Get a Google Sheet range.", "GET", "/v4/spreadsheets/{spreadsheet_id}/values/{range}", base_url="https://sheets.googleapis.com", required_params=("spreadsheet_id", "range")),
            "sheets_update": _action("sheets_update", "Update a Google Sheet range.", "PUT", "/v4/spreadsheets/{spreadsheet_id}/values/{range}", base_url="https://sheets.googleapis.com", write=True, required_params=("spreadsheet_id", "range", "values"), optional_params=("majorDimension",), query_params=("valueInputOption",), approval_note="Writes spreadsheet cells."),
        },
        notes="OAuth scopes vary by product; status and dry-run planning are available by default.",
    ),
    "notion": IntegrationDefinition(
        integration_id="notion",
        display_name="Notion",
        category="productivity",
        credential_groups=(("NOTION_TOKEN", "NOTION_API_KEY", "notion_token"),),
        base_url="https://api.notion.com",
        docs_url="https://developers.notion.com/reference/intro",
        setup_steps=(
            "Create a Notion internal integration.",
            "Share the target pages/databases with the integration.",
            "Store the secret as NOTION_TOKEN or NOTION_API_KEY.",
        ),
        actions={
            "search": _action("search", "Search pages and databases visible to the integration.", "POST", "/v1/search"),
            "retrieve_page": _action("retrieve_page", "Retrieve a Notion page.", "GET", "/v1/pages/{page_id}", required_params=("page_id",)),
            "create_page": _action("create_page", "Create a Notion page.", "POST", "/v1/pages", write=True, required_params=("parent", "properties"), optional_params=("children", "icon", "cover"), approval_note="Creates a Notion page."),
            "update_page": _action("update_page", "Update Notion page properties.", "PATCH", "/v1/pages/{page_id}", write=True, required_params=("page_id",), optional_params=("properties", "archived", "icon", "cover"), approval_note="Updates Notion content."),
            "query_database": _action("query_database", "Query a Notion database.", "POST", "/v1/databases/{database_id}/query", required_params=("database_id",), optional_params=("filter", "sorts", "page_size")),
        },
    ),
    "slack": IntegrationDefinition(
        integration_id="slack",
        display_name="Slack",
        category="communication",
        credential_groups=(("SLACK_BOT_TOKEN", "SLACK_USER_TOKEN", "slack_bot_token"),),
        base_url="https://slack.com",
        docs_url="https://api.slack.com/methods",
        setup_steps=(
            "Create a Slack app and install it to your workspace.",
            "Grant only the scopes Cato needs, such as chat:write and channels:read.",
            "Store the bot token as SLACK_BOT_TOKEN.",
        ),
        actions={
            "list_channels": _action("list_channels", "List Slack conversations.", "GET", "/api/conversations.list", query_params=("types", "limit", "cursor")),
            "get_channel_history": _action("get_channel_history", "Read recent Slack channel history.", "GET", "/api/conversations.history", required_params=("channel",), query_params=("limit", "oldest", "latest")),
            "post_message": _action("post_message", "Post a Slack message to a channel.", "POST", "/api/chat.postMessage", write=True, required_params=("channel", "text")),
            "schedule_message": _action("schedule_message", "Schedule a Slack message.", "POST", "/api/chat.scheduleMessage", write=True, required_params=("channel", "text", "post_at"), approval_note="Schedules a user-visible Slack message."),
        },
    ),
    "discord": IntegrationDefinition(
        integration_id="discord",
        display_name="Discord",
        category="communication",
        credential_groups=(("DISCORD_BOT_TOKEN", "DISCORD_TOKEN", "discord_bot_token"),),
        base_url="https://discord.com/api/v10",
        docs_url="https://discord.com/developers/docs/intro",
        setup_steps=(
            "Create a Discord application and bot.",
            "Invite it only to servers/channels where Cato should operate.",
            "Store the bot token as DISCORD_BOT_TOKEN.",
        ),
        actions={
            "get_channel": _action("get_channel", "Get a Discord channel.", "GET", "/channels/{channel_id}", required_params=("channel_id",), auth="bot"),
            "list_channel_messages": _action("list_channel_messages", "List Discord channel messages.", "GET", "/channels/{channel_id}/messages", required_params=("channel_id",), query_params=("limit",), auth="bot"),
            "send_message": _action("send_message", "Send a message to a Discord channel.", "POST", "/channels/{channel_id}/messages", write=True, required_params=("channel_id", "content"), auth="bot"),
        },
    ),
    "telegram": IntegrationDefinition(
        integration_id="telegram",
        display_name="Telegram",
        category="communication",
        credential_groups=(("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),),
        base_url="https://api.telegram.org",
        docs_url="https://core.telegram.org/bots/api",
        setup_steps=(
            "Create a Telegram bot with BotFather.",
            "Store the bot token as TELEGRAM_BOT_TOKEN.",
            "Only send messages to approved chat IDs.",
        ),
        actions={
            "get_me": _action("get_me", "Check Telegram bot identity.", "GET", "/bot{token}/getMe", auth="telegram_bot"),
            "send_message": _action("send_message", "Send a Telegram bot message.", "POST", "/bot{token}/sendMessage", write=True, required_params=("chat_id", "text"), auth="telegram_bot"),
            "send_document": _action("send_document", "Send a Telegram document by URL or file id.", "POST", "/bot{token}/sendDocument", write=True, required_params=("chat_id", "document"), optional_params=("caption",), auth="telegram_bot", approval_note="Sends a file/message to Telegram."),
        },
    ),
    "whatsapp": IntegrationDefinition(
        integration_id="whatsapp",
        display_name="WhatsApp",
        category="communication",
        credential_groups=(("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_TOKEN", "whatsapp_access_token"), ("WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_PHONE_ID", "whatsapp_phone_number_id")),
        base_url="https://graph.facebook.com",
        docs_url="https://developers.facebook.com/docs/whatsapp/cloud-api",
        setup_steps=(
            "Create/configure a Meta WhatsApp Cloud API app.",
            "Store the access token as WHATSAPP_ACCESS_TOKEN or WHATSAPP_TOKEN.",
            "Store the phone number id as WHATSAPP_PHONE_NUMBER_ID or WHATSAPP_PHONE_ID.",
            "Keep outbound messages approval-gated.",
        ),
        actions={
            "send_text": _action("send_text", "Send a WhatsApp Cloud API text message.", "POST", "/v20.0/{phone_number_id}/messages", write=True, required_params=("phone_number_id", "to", "text"), optional_params=("messaging_product", "type"), approval_note="Sends a WhatsApp message."),
        },
        notes="WhatsApp Cloud API requires a phone number id and approved recipient/session rules.",
    ),
}


def list_integrations() -> list[IntegrationDefinition]:
    """Return all supported integration definitions."""
    return [_INTEGRATIONS[key] for key in sorted(_INTEGRATIONS)]


def get_integration(integration_id: str) -> IntegrationDefinition | None:
    """Return an integration definition by id."""
    return _INTEGRATIONS.get(integration_id.strip().lower())
