# Mealie MCP Server

[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/rldiao-mealie-mcp-server-badge.png)](https://mseep.ai/app/rldiao-mealie-mcp-server)

A Model Context Protocol (MCP) server that connects AI assistants to your
[Mealie](https://github.com/mealie-recipes/mealie) recipe database. One
deployment serves many people, and each tool call runs with the calling user's
own Mealie credentials.

## Contents

- [Features](#features)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Docker](#docker)
- [Usage Examples](#usage-examples)
- [Available Tools](#available-tools)
- [Development](#development)
- [Important Notes](#important-notes)
- [Support and Contributing](#support-and-contributing)
- [License and Credits](#license-and-credits)

## Features

- **Recipes:** Create, read, update, import, duplicate, and delete recipes.
- **Search:** Filter by text, categories, tags, and tools with AND/OR logic.
- **Images and assets:** Upload recipe images and files, or set images from URLs.
- **Nutrition and display:** Set per-serving nutrition and recipe visibility
  settings such as `showAssets` and `showNutrition`.
- **Ingredients:** Resolve free-text ingredients against Mealie's food and unit
  vocabulary.
- **Shopping lists:** Manage lists and items, perform bulk operations, and add
  recipe ingredients with quantity scaling.
- **Organization:** Manage categories, tags, foods, units, and recipe tools; find
  unused categories and tags.
- **Meal planning:** View, create, update, and delete meal plan entries, create
  multiple entries, and mark recipes as made today.

## Quick Start

The server is built for a shared deployment. It runs over HTTP behind an MCP
gateway that authenticates each person. Every tool call then runs as that
person in Mealie, with their own household and permissions. No Mealie token is
shared between users.

```text
MCP client ──OAuth──▶ gateway (validates JWT, sets X-User) ──▶ mealie-mcp ──▶ Mealie
                                                                  │
                                         Authentik API (user attribute mealie_token)
```

### Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/),
  or Docker
- A running Mealie instance
- Authentik as the identity provider, and an MCP gateway such as
  [agentgateway](https://agentgateway.dev) that validates Authentik tokens

### How a Request Is Authorized

1. The gateway validates the user's Authentik JWT. It writes a verified claim
   (`preferred_username`) into the `X-User` header and overwrites any
   client-supplied value.
2. The server reads `X-User` on each tool call. It looks up that Authentik user
   and reads the Mealie API token from the user attribute `mealie_token`, using
   a read-only service account.
3. The token is cached per user for `MEALIE_TOKEN_CACHE_TTL` seconds (default
   120). Deactivating the user or removing the attribute takes effect after at
   most that long. If Mealie rejects a token with HTTP 401, the cached copy is
   dropped right away.
4. The tool calls Mealie with that user's token.

Tool calls fail with a clear message when no user header is present, when no
token is linked ("No Mealie token linked to your account; ask the admin to set
it up."), or when Mealie rejects the token (401) or the action (403).

Because the server trusts `X-User`, it must be reachable **only** through the
gateway. Run it on an internal network with no published port. Anyone who can
reach it directly can act as any user.

### Onboarding a User

1. In Mealie, create an API token for the user. As an admin, use impersonation,
   or have the user create one under their profile.
2. In Authentik, open the user and add the token to their attributes:

   ```yaml
   mealie_token: <the user's Mealie API token>
   ```

3. Grant the Authentik service account read access to users. A dedicated
   service account with only the "Can view User" permission is enough. Use its
   API token as `AUTHENTIK_TOKEN`.

### Gateway Configuration (agentgateway)

Validate Authentik tokens with `mcpAuthentication`, then overwrite `X-User` with
the verified claim. agentgateway's `set` replaces any client-supplied value. If
the claim is missing, the expression fails and agentgateway removes the header,
so a client cannot forge it.

```yaml
policies:
  mcpAuthentication:
    issuer: https://auth.example.com/application/o/mealie-mcp/
    audiences: [<authentik client id>]
    jwks:
      url: https://auth.example.com/application/o/mealie-mcp/jwks/
    # resourceMetadata: ... as required by your MCP clients
  transformations:
    request:
      set:
        x-user: jwt.preferred_username
```

## Configuration

Set environment variables in the container or shell. For a local checkout, you
can also copy [`.env.template`](.env.template) to `.env`. Never commit tokens.

| Variable | Default | Description |
| --- | --- | --- |
| `MEALIE_BASE_URL` | Required | Mealie base URL, including protocol and port if needed |
| `AUTHENTIK_URL` | Required | Authentik base URL, e.g. `https://auth.example.com` |
| `AUTHENTIK_TOKEN` | Required | API token of a read-only Authentik service account |
| `AUTHENTIK_TOKEN_ATTRIBUTE` | `mealie_token` | User attribute that holds the Mealie API token |
| `MCP_USER_HEADER` | `X-User` | Header the gateway fills with the verified username |
| `MEALIE_TOKEN_CACHE_TTL` | `120` | Seconds to cache a user's token, from 0 to 3600 |
| `MEALIE_ENABLE_AI_IMPORT` | `false` | Opt in to AI recipe import; accepts `true`/`false` (case-insensitive) |
| `MCP_TRANSPORT` | `streamable-http` | `streamable-http` or legacy `sse` |
| `MCP_HOST` | `127.0.0.1` | HTTP bind address; use `0.0.0.0` in containers |
| `MCP_PORT` | `8765` | HTTP port, from 1 to 65535 |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |

`MEALIE_API_KEY` is no longer supported. Startup fails while it is set, so a
shared token cannot stay in use by mistake. The stdio transport is not supported
either, because it carries no per-user identity.

Streamable HTTP runs stateless: every request stands on its own, so connected
clients survive server restarts and redeploys without reconnecting. Legacy SSE
keeps per-connection state and does not.

At startup, the server checks that Mealie is healthy and that Authentik accepts
the service-account token. It exits if either check fails. The MCP endpoint is
`http://<host>:<port>/mcp`.

## Docker

The [Dockerfile](Dockerfile) runs the server as a persistent container.

### Prebuilt Image

Multi-arch (`linux/amd64`, `linux/arm64`) images are published to the GitHub
Container Registry on every push to `main` (`latest`, `main`, `sha-<short>`) and
for `v*` tags (`X.Y.Z`, `X.Y`):

```bash
docker pull ghcr.io/jeroenluchtenberg/mealie-mcp-server:latest
```

Use this image in place of `mealie-mcp-server` in the examples below, or build
it locally.

### Build

```bash
docker build -t mealie-mcp-server .
```

### Docker Compose

Put the server on an internal network shared only with the gateway and Mealie.
Do not publish its port. Set `AUTHENTIK_TOKEN` in the shell or the Compose `.env`
file.

```yaml
services:
  mealie-mcp:
    image: ghcr.io/jeroenluchtenberg/mealie-mcp-server:latest
    container_name: mealie-mcp
    restart: unless-stopped
    environment:
      MEALIE_BASE_URL: http://mealie:9000
      AUTHENTIK_URL: https://auth.example.com
      AUTHENTIK_TOKEN: ${AUTHENTIK_TOKEN}
      MCP_HOST: "0.0.0.0"
    expose:
      - "8765"
    networks:
      - mcp_internal

networks:
  mcp_internal:
    internal: true
```

`expose` does not publish a host port. Attach the gateway (and Mealie, or a
route to it) to `mcp_internal`, and point the gateway's MCP target at
`http://mealie-mcp:8765/mcp`. An `internal: true` network has no outbound
access. If Authentik is reachable only through another network, attach the
server to that network as well, but never publish its port.

## Usage Examples

```text
"Search for chicken recipes"
"Create a new recipe for pasta carbonara"
"Mark the meatloaf recipe as made today"
"Create a shopping list for this week"
"Add all ingredients from the lasagna recipe to my shopping list"
"Plan chicken soup for lunch on Friday"
```

See [Usage Examples](USAGE_EXAMPLES.md) for detailed workflows and troubleshooting.

## Available Tools

### Recipe Tools (12 operations)

- `get_recipes` - List/search recipes with advanced filtering
- `get_recipe` - Get complete recipe details, or a summary with `concise=true`
- `create_recipe` - Create a recipe; name and instructions are required (`[]`
  for a recipe without steps), with optional ingredients, metadata, nutrition,
  and display settings. Ingredient strings are parsed into existing units and
  foods; lines without a matching food are returned as `unmatched_ingredients`
- `import_recipe_from_url` - Import a recipe from a web page
- `update_recipe` - Update content or metadata, including nutrition and display
  settings; omitted fields are preserved, and empty lists clear content
- `duplicate_recipe` - Clone a recipe
- `mark_recipe_last_made` - Update last made timestamp
- `set_recipe_image_from_url` - Set image from URL
- `upload_recipe_image_file` - Upload image file
- `upload_recipe_asset_file` - Upload document/asset
- `update_recipe_categories_and_tags` - Replace or clear categories, tags, or both using IDs
- `delete_recipe` - Delete recipe

### Shopping List Tools (15 operations)

- `get_shopping_lists` - List all shopping lists
- `create_shopping_list` - Create new list
- `get_shopping_list` - Get list by ID
- `update_shopping_list` - Rename a list while preserving other fields
- `delete_shopping_list` - Delete list
- `add_recipe_to_shopping_list` - Add recipe ingredients
- `remove_recipe_from_shopping_list` - Remove recipe ingredients
- `get_shopping_list_items` - List all items
- `get_shopping_list_item` - Get item by ID
- `create_shopping_list_item` - Create single item
- `create_shopping_list_items_bulk` - Create multiple items
- `update_shopping_list_item` - Update item (preserves fields)
- `update_shopping_list_items_bulk` - Update multiple items
- `delete_shopping_list_item` - Delete single item
- `delete_shopping_list_items_bulk` - Delete multiple items

### Category Tools (6 operations)

- `get_categories` - List/search categories
- `get_empty_categories` - Find unused categories
- `create_category` - Create new category
- `get_category` - Get by exactly one of `category_id` or `category_slug`
- `update_category` - Update category
- `delete_category` - Delete category

### Tag Tools (6 operations)

- `get_tags` - List/search tags
- `get_empty_tags` - Find unused tags
- `create_tag` - Create new tag
- `get_tag` - Get by exactly one of `tag_id` or `tag_slug`
- `update_tag` - Update tag
- `delete_tag` - Delete tag

### Food Tools (5 operations)

- `get_foods` - List/search foods (resolve IDs for structured ingredients)
- `create_food` - Create a new food
- `get_food` - Get by ID
- `update_food` - Update food
- `delete_food` - Delete food

### Unit Tools (5 operations)

- `get_units` - List/search units
- `create_unit` - Create a new unit
- `get_unit` - Get by ID
- `update_unit` - Update unit
- `delete_unit` - Delete unit

### Kitchen Tools (5 operations)

- `get_tools` - List/search recipe tools (includes `householdsWithTool`)
- `create_tool` - Create a new tool
- `get_tool` - Get by exactly one of `tool_id` or `tool_slug`
- `update_tool` - Update tool
- `delete_tool` - Delete tool

### Parser Tools (2 operations)

- `parse_ingredients` - Resolve one or more ingredient lines in one request; always returns a list
- `resolve_ingredients` - Match unmatched food text to existing foods (suggest),
  then add an alias or create a food, with duplicate guards

### Meal Plan Tools (6 operations)

- `get_all_mealplans` - List meal plans
- `create_mealplan` - Create meal plan entry
- `create_mealplan_bulk` - Create multiple entries
- `update_mealplan` - Update an entry while preserving omitted fields
- `delete_mealplan` - Delete an entry
- `get_todays_mealplan` - Get today's meals

### User Tools (1 operation)

- `whoami` - Show the current user's username, household, and group

**Total: 63 tools**

With `MEALIE_ENABLE_AI_IMPORT=true`, `import_recipe_with_ai` adds one optional
recipe operation: **64 total tools, including 13 recipe tools**. It is absent
from discovery and cannot be called when disabled.

### Migrating consolidated tools (breaking change)

Redundant MCP names have been removed, not retained as aliases. Refresh your
client's tool list and update saved calls:

| Removed tool | Replacement |
| --- | --- |
| `create_recipe_full` | `create_recipe` with the same arguments |
| `get_recipe_detailed` | `get_recipe` (full details by default) |
| `get_recipe_concise` | `get_recipe` with `concise=true` |
| `patch_recipe` | `update_recipe` with the same arguments |
| `set_recipe_categories` | `update_recipe_categories_and_tags` with `category_ids` |
| `set_recipe_tags` | `update_recipe_categories_and_tags` with `tag_ids` |
| `get_category_by_slug` | `get_category` with `category_slug` |
| `get_tag_by_slug` | `get_tag` with `tag_slug` |
| `get_tool_by_slug` | `get_tool` with `tool_slug` |
| `parse_ingredient` | `parse_ingredients(ingredients=[...])`; read the first result |

Existing `create_recipe` and `update_recipe` calls remain supported. Recipe
updates can now combine content and metadata, with omitted or null fields left
unchanged. Ingredient and instruction lists replace only the provided fields.
Nutrition remains a whole-object replacement, while settings are merged.

Distinct operations remain separate: single and bulk writes have different
response/failure contracts; paginated lists differ from individual lookups,
unused-organizer queries, and today's meal plans. Recipe URL import, image URL
scraping, and file uploads also perform different operations.

### Optional AI recipe import

Set `MEALIE_ENABLE_AI_IMPORT=true` in the server's environment or local `.env`.
Restart the server and refresh the client's tool list. This applies to SSE and
Streamable HTTP. Offline SDK discovery remains configuration-
and network-free, listing only default tools; optional registration occurs at
runtime startup (or when passing an explicit enabled `ServerConfig`).

`import_recipe_with_ai` requires **Mealie 3.23.0+** and a default AI provider
configured for the calling user's group. It accepts any combination of:

- `content`: plain text, raw HTML, or JSON; also used for corrections or notes.
- `url`: an HTTP(S) recipe or video URL, fetched by Mealie and saved as the source.
- `image_paths`: ordered image paths accessible to the **MCP server's filesystem**,
  not a remote caller's computer. Multiple photos become one recipe; the first
  becomes its cover image.

At least one nonblank source is required. Sources are combined, with pasted
content taking precedence when they disagree. Optional `translate_language`
requests translation. `create_new_organizers` defaults to `false`: matching
existing tags, categories, and kitchen tools may be assigned, but new ones are
created only when explicitly requested.

Before each import the server reads `/api/groups/self` to check `aiEnabled` and,
for photos, `imageProviderEnabled`. Missing/malformed capabilities or a failed
lookup produce an explicit error, not a silent disabled result. Video detection
and the audio-provider requirement are handled by Mealie. These checks establish
configuration, not provider connectivity, credentials, or available quota.

**This tool immediately creates and saves a recipe.** Source material is processed
by Mealie's configured AI providers and may incur charges. Review the returned
recipe for accuracy. The import has a 300-second read timeout; other API calls
retain their existing timeouts. No imports are automatically retried. After a
timeout or connection failure, check Mealie before retrying or switching tools:
creation may already have succeeded. If the follow-up fetch fails, the error
includes `created_slug` and `stage`; retrieve that recipe rather than importing again.

Choose the tool according to the task:

| Task | Tool |
| --- | --- |
| Ordinary recipe webpage | `import_recipe_from_url` |
| Unstructured text, photos, video, combined sources, translation, or explicit AI import | `import_recipe_with_ai` (opt-in) |
| Save already-composed ingredients and instructions | `create_recipe` |
| Attach a photo to an existing recipe without extracting content | `upload_recipe_image_file` |

The opt-in controls only this new tool. It does not disable Mealie's own AI
fallback for URL scraping or other existing AI features. See
[Mealie's AI import documentation](https://mealie.io/documentation/getting-started/installation/ai-providers/#import-with-ai)
and [usage examples](USAGE_EXAMPLES.md#optional-ai-import).

## Development

### Setup

Clone the repository and install development dependencies:

```bash
git clone https://github.com/Jeroenluchtenberg/mealie-mcp-server.git
cd mealie-mcp-server
uv sync --locked --extra dev
```

For manual testing against a real instance, fill in `.env` and start the server:

```bash
cp .env.template .env
# Edit .env with your Mealie and Authentik details
uv run mealie-mcp-server
```

Connect the [MCP Inspector](https://github.com/modelcontextprotocol/inspector)
to `http://127.0.0.1:8765/mcp` with Streamable HTTP and add an `X-User` header
for a user that has a linked token. Only do this on a loopback address: without
the gateway, the header is not verified.

Run the offline checks; these do not require a Mealie instance or credentials:

```bash
uv run ruff check src tests
uv run pytest -q
```

### Project Structure

| Path | Purpose |
| --- | --- |
| [`src/mealie/`](src/mealie/) | HTTP client and API mixins |
| [`src/tools/`](src/tools/) | FastMCP tool definitions and registration |
| [`src/models/`](src/models/) | Pydantic request and response models |
| [`src/server.py`](src/server.py) | Configuration, lifecycle, and entry point |
| [`src/identity.py`](src/identity.py) | Per-request user identity and Authentik token lookup |
| [`src/prompts.py`](src/prompts.py) | MCP prompts |
| [`tests/`](tests/) | Offline tests and fixtures |

Repository conventions are in [AGENTS.md](AGENTS.md), with focused guidance for
[source code](src/AGENTS.md) and [tests](tests/AGENTS.md).

The automated suite uses fake HTTP responses and includes local HTTP-transport
checks. It does not replace compatibility testing against your deployed Mealie
version.

## Important Notes

The calls below use Python-style notation to illustrate MCP tool arguments; they
are not standalone Python scripts.

### Filtering by Tags and Categories

When filtering recipes, you **must use slugs or UUIDs**, not display names:

Use `get_tags()` or `get_categories()` first to find the correct slugs:

```python
get_recipes(tags=["quick-meals", "healthy"])
```

For example, pass `quick-meals`, not the display name `Quick Meals`.

### Filtering by Household

Recipes are shared across all households in a Mealie group, so `get_recipes`
returns every household's recipes by default. Filter with household ids or
slugs, or `"self"` for the caller's own household:

```python
get_recipes(households=["self"])
```

Unknown households return an error rather than an unfiltered list. Concise
results include each recipe's `household` slug. Meal plans and shopping lists
are already scoped to the caller's household.

### Nutrition Is Replaced, Not Merged

Mealie replaces the whole `nutrition` object on write. `update_recipe` follows
suit, so pass every value you want to keep:

```python
# Clears every nutrition value except fat.
update_recipe(slug="...", nutrition={"fatContent": "12"})
```

### Parsing Ingredients in Bulk

Resolving ingredients by hand costs one to two `get_foods` / `get_units` calls
each. `parse_ingredients` does a whole recipe in one request and returns results
that can be handed straight to `create_recipe`:

```python
parse_ingredients(ingredients=["1/4 cup chopped onion", "2 large eggs"])
# -> [{"input": "1/4 cup chopped onion", "confidence": 0.99, "quantity": 0.25,
#      "unit": {"id": "...", "name": "cup"},
#      "food": {"id": "...", "name": "onion"}, "note": "chopped"}, ...]
```

A `null` unit means your instance has no matching unit. When the parser finds
no food, `parse_ingredients` compares the food text with every existing food
name, plural, and alias (one food-list request per call, no writes). An exact
match is linked directly and marked `"resolved": "auto"`. Anything else keeps
`food.id` null and gets a `suggestion` in the same shape `resolve_ingredients`
returns:

```python
parse_ingredients(ingredients=["zout naar smaak"])
# -> [{..., "food": {"id": null, "name": "zout naar smaak"},
#      "suggestion": {"text": "zout naar smaak", "status": "suggested",
#                     "candidates": [...],
#                     "suggestion": {"action": "use_with_note",
#                                    "food": {"id": "...", "name": "zout"},
#                                    "note": "naar smaak"}}}]
```

Suggestions are never applied automatically: a word-level match can be a
different ingredient (`zoete aardappel` is not `aardappel`). Confirm them, then
link the food or apply the alias/create decision with `resolve_ingredients`.
Pass `verbose=True` for Mealie's full response including per-field confidences,
plus the suggestion.

`create_recipe` and `update_recipe` run plain ingredient strings through the
same parser. Decimal commas such as `0,25 tl` are read as `0.25`. When a line
has no matching food, its quantity and unit are kept and the food text goes
into the note. Such lines are listed in `unmatched_ingredients` in the result
so they can be resolved and written back with `update_recipe`. To review
suggested foods before saving, run the lines through `parse_ingredients` first
and pass the confirmed results as structured ingredients.

### Resolving Unmatched Foods Without Duplicates

`resolve_ingredients` works in two passes. Use it before creating foods, because
duplicates such as `selderij` / `bleekselderij` / `stengel selderij` stop shopping
lists from merging items.

```python
resolve_ingredients(items=[{"text": "stengel bleekselderij"}, {"text": "zout naar smaak"}])
# -> suggestion use_with_note: food "bleekselderij", note "stengel"
#    suggestion use_with_note: food "zout", note "naar smaak"

resolve_ingredients(items=[
    {"text": "laurierbladeren", "action": "alias", "food_id": "<laurierblad id>"},
    {"text": "pandanblad", "action": "create"},
])
```

The default `suggest` action only reads. It loads the food catalogue once per
call and compares each text with every food name, plural, and alias: as a
whole and word by word, by similarity, and as a Dutch compound suffix
(`bleekselderij` ends in `selderij`). Mealie's own search only finds
substrings, so it would miss `laurierbladeren` → `laurierblad`. It suggests `use` (an exact
match), `use_with_note` (an existing food appears in the text; the remaining
words become the note), `alias` (a near-identical name or a compound ending in
one; check the candidates, since `knolselderij` is not `selderij`), or `create`. The
`alias` action makes the parser match the text from then on. `create` returns
`exists` instead of making a duplicate, and `alias` returns `conflict` when
another food already owns that name.

Creating foods or aliases needs the Mealie permission **can organize group
data**. Without it, Mealie returns HTTP 403. The item then reports that
permission and the remaining writes are skipped.

### Uploaded Assets and Nutrition Can Be Stored but Hidden

A recipe's `settings` object controls what the UI renders. `showAssets` and
`showNutrition` gate the assets and nutrition cards, so an asset uploaded with
`upload_recipe_asset_file` can be present in the API response and still be
invisible in the web UI. Flip the toggle with:

```python
update_recipe(slug="...", settings={"showAssets": True})
```

Mealie seeds a new recipe's settings from the household preferences
(`recipeShowAssets`, `recipeShowNutrition`, ...), so the defaults differ per
instance. Check rather than assume.

Only the toggles you pass are changed. The tool reads the recipe's current
settings and sends the merged object, because Mealie does not reliably preserve
toggles omitted from a settings PATCH.

### Field Preservation

When updating shopping list items, both single and bulk updates fetch the current
records and preserve omitted fields. You only need to specify the fields you
want to change:

```python
# Only updates 'checked' field, preserves note, quantity, etc.
update_shopping_list_item(item_id="...", checked=True)
```

Bulk shopping inputs accept snake_case names such as `shopping_list_id` and
Mealie's camelCase names such as `shoppingListId`. Conflicting aliases and
duplicate IDs in a bulk update are rejected before writing.

### Meal Plan Validation and Clearing

Meal dates must use `YYYY-MM-DD`, and entry types must be `breakfast`, `lunch`,
`dinner`, or `side`. Creation requires a recipe or a nonblank title. Bulk meal
plans are validated in full before any entries are created.

Omitted update fields remain unchanged. To remove an existing recipe link,
use `update_mealplan(entry_id="...", clear_recipe=True, title="Leftovers")`.
Do not combine `clear_recipe` with a replacement `recipe_id`.

### Recovering from Partially Completed Writes

Recipe creation/population and bulk meal-plan creation require multiple API
requests and are not atomic. If a later request fails, the tool error includes
recovery information identifying completed work. Inspect that information and
the current Mealie state rather than blindly retrying the entire operation.
A failed or timed-out request may have completed remotely.

## Support and Contributing

- Check the [changelog](CHANGELOG.md) for changes and migration notes.
- Review the [usage guide](USAGE_EXAMPLES.md) and
  [Mealie documentation](https://docs.mealie.io).
- Report problems through [GitHub issues](https://github.com/rldiao/mealie-mcp-server/issues).
- For pull requests, follow the [development workflow](#development) and include
  tests for behavior changes.

## License and Credits

Licensed under the [MIT License](LICENSE).

- [Mealie](https://github.com/mealie-recipes/mealie) - The recipe management system
- [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk) - SDK and
  bundled FastMCP server
- [Model Context Protocol](https://modelcontextprotocol.io) - Protocol documentation
- [Claude Desktop](https://claude.ai/download)
