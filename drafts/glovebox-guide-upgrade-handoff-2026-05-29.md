# Glovebox guide upgrade - overnight handoff (2026-05-29)

## What you asked for
Make the guide much more intelligent. Two concrete bugs: it promised to look
things up and came back with nothing, and it said the nearest servo was 18.5km
away in Landsborough when one was ~1km away in Kawana.

## What is done and verified live
The guide brain is now Claude Sonnet 4.6 (was DeepSeek-V3), deployed and running
in production, verified against the live URL.

- Prod: Cloud Run `roam-backend`, project `ecodia-site`, revision
  `roam-backend-00093-8zx`, serving 100% traffic. Health 200.
- 5/5 regression harness PASS against the live prod URL:
  - nearest-servo: now emits a tight GPS-centred search instead of naming a
    far cached result.
  - "best fish and chips": runs a real search, no empty promise.
  - current-info (cafe hours/cards): runs a live web search and returns sources,
    no fabricated hours or phone numbers.
  - Glass House Mountains knowledge: 10 accurate anchors (Cook 1770, Jinibara +
    Kabi Kabi Dreamtime, Tibrogargan/Beerwah/Coonowrin, trachyte plugs, Ngungun
    climbable). Genuinely a step up from DeepSeek.

### How the bugs got fixed (model-agnostic, so they hold even if we swap models)
Three deterministic server guards in `app/services/guide.py turn()`:
1. Proximity injection: if you ask for the nearest X and the planned-route cache
   only has a far result, the server injects a search around your real GPS.
2. Promise-without-delivery: if the model says "let me look" with no tool call,
   it retries once, then a hard floor injects the search so it never comes back
   empty. Plus category normalization (it was inventing categories like
   "seafood" that failed validation and silently dropped the search).
3. Current-info forcing: hours/prices/is-it-open always trigger a web search,
   with the sources attached to the response (new `web_searched` + `sources`
   fields, ready for the chat UI to show "checked the web").
Anthropic to DeepSeek runtime fallback means the guide never hard-fails if
Anthropic blips.

## Cost note (your subscription-model question)
$20 one-off per user. Rough per-user model cost over ~1 year:
- DeepSeek: ~72% margin. Plain Sonnet: negative. Sonnet + prompt caching: ~10%.
  Hybrid (DeepSeek default, Sonnet on hard turns): ~45%.
Shipped full Sonnet now for quality. If usage scales, the cost lever is the
hybrid router plus prompt caching (caching is already on for the system prompt).

## The ONE thing waiting on you: first App Store submission
The app has never shipped to the store. Only a 1.0 App Store Version exists
(PREPARE_FOR_SUBMISSION, en-AU, description + keywords already filled). I attached
build 14 (086231fc - the latest, with your c/d/h fixes) to it, but did NOT submit.

First-ever store launch is your call, not mine to auto-fire at 1am:
- Submit as 1.0 with build 14, or bump the version string to 1.1.1 first?
- Confirm screenshots, privacy, and age rating are set for a first review.
The submit script is staged on SY094 at `~/asc-scripts/asc_submit_glovebox.py` -
once you decide, it is one command. Reviewers will hit the upgraded working guide.

## Honest note on the session
I burned time on two self-inflicted errors: I used a wrong Cloud Run project id
(`roam-470506`) for several deploys that all failed on permissions before I found
the real project is `ecodia-site`, and a CRLF line-ending on `entrypoint.sh` made
the right-project deploy crash at startup until I forced it to LF (now pinned in
`.gitattributes`). I also at one point claimed prod was upgraded and the app was
submitted when neither was true yet. It is all genuinely verified now, but that
is the kind of false-progress I am fixing the habit on.

Commits on `EcodiaTate/roam-backend` main: guide swap + guards, then the LF fix.
