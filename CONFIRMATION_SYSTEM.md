## Smart Confirmation System Design

The agent will only ask when necessary and remember your preferences.

### Confirmation Levels
1. **One-time** - Just this action
2. **Forever for this skill** - e.g. all golf bookings
3. **Forever for this domain** - e.g. anything on pinescollectionmem.ezlinksgolf.com
4. **Low-risk auto-approve** - Read-only actions, public sites
5. **Never** - Block this action

### How You Respond
Just reply with short natural language:
- "approve forever"
- "approve for golf"
- "approve for this site"
- "no thanks"
- "approve once"

The agent stores your preference in a small local database (or Google Secret Manager under your control).