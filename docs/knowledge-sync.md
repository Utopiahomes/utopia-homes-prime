# Keeping Lucy's knowledge in step with the website

Lucy answers only from an approved knowledge release (`knowledge/releases.json`). The facts about
each home come from the website, so they stay in step automatically, with Ray approving each
change.

```
utopia-homes-web content/properties.ts
   └─ deployed site publishes  https://www.utopiahomes.com/lucy-knowledge/properties.json
        └─ Knowledge sync workflow (daily, or run by hand)       tools/knowledge_sync.py
             └─ opens/refreshes one pull request: "Lucy knowledge update: homes-knowledge:rN"
                  └─ Ray merges it  = approval (closing it = rejection)
                       └─ Homes Prime deploys; KNOWLEDGE_RELEASE_ID=latest-approved serves rN
```

- **What syncs:** every active property's page facts: description, city, capacity, rooms,
  amenities, highlights, parking, pet policy, and accessibility. Each home becomes 7 entries.
  The structured facts Lucy checks answers against (guests, parking spaces, pool, hot tub,
  bedrooms, bathrooms, dogs) are derived from the same text.
- **What stays hand-maintained:** everything that is not about one home (about, booking, design,
  membership, owners, contact, destination) lives in `knowledge/base/homes-base-entries.json`.
  Edit it in a pull request, and the next sync proposes the release. The collection overview is
  rewritten from the active homes.
- **No change, no release:** an unchanged website reproduces the approved release byte for byte.
  If the website changes back before the pull request is merged, the workflow closes it.
- **Limits:** the admitted evidence packet holds at most 64 entries (`MAX_ADMITTED_IDS`). With
  7 entries per home plus 10 base entries, that is about 7 homes. Beyond that, the packet needs
  selecting by page or question.

Run it locally:

    python tools/knowledge_sync.py                       # has the website changed?
    python tools/knowledge_sync.py --source feed.json    # against a saved feed
    python tools/knowledge_sync.py --propose             # write the next release and register entry

Production serves the latest approved release with:

    GUEST_ANSWER_PROVIDER_KNOWLEDGE_RELEASE_ID=latest-approved
    GUEST_ANSWER_PROVIDER_HOMES_PRIME_KNOWLEDGE_REGISTER=/app/knowledge/releases.json

and without `HOMES_PRIME_KNOWLEDGE_PATH` or `HOMES_PRIME_KNOWLEDGE_ALLOWED_DIGESTS` (the service
refuses to start if either is also set). The digest pin still applies; it comes from the
register. The explicit three-variable pin from `tools/knowledge_release.py pin` still works for
holding production on a specific release.
