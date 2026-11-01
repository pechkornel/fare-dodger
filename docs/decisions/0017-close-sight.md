# 0017. Seeing the other side only up close

- Status: accepted
- Date: 2026-10-29

## Context
The Dodger saw every Bounty Hunter on the map at all times, and could plan around them from
far away. The owner wants it tense on both sides: the Dodger only knows how far the nearest
Bounty Hunter is, shown in red, green or neutral on the top bar, and both sides see each other
only when they are quite zoomed in.

## Decision
- **`see_radius_m` (200 m).** A Dodger in play is sent only the Bounty Hunters within that
  distance of themselves. A Bounty Hunter's team sees a Dodger on foot within that distance of
  any of them (this was `sight_radius_m`, 100 m). Bots still give chase within
  `sight_radius_m`.
- **Zoomed in only.** Below the toy-town zoom (`SWITCH_ZOOM`, 15.5) the client hides the
  other side's markers, the Bounty Hunters' sight circles (for the Dodgers), and the Dodgers'
  dots on the Bounty Hunters' minimap. The Dodgers' minimap never showed the Bounty Hunters.
- **The distance.** `nearest_hunter_m` goes on the top bar for the Dodger: red within 1.5x
  `see_radius_m` (300 m), green beyond 4x (800 m), neutral in between.
- Once caught, or when the round is over, a Dodger sees everyone again.

## Consequences
- The bot Dodger still plans with full knowledge of the Bounty Hunters. That's a known
  advantage, left for the balance pass. (Fixed in ADR 0019: it now sees only what a human
  Dodger sees.)
- The zoom limit is enforced only in the client. The server sends just the close ones, so
  zooming in on a far street shows nothing new.

## Addendum: balance on the bigger districts
200 bot rounds on Inner Pest, after the 1.5x wider districts, at the live config's random
start times. The bot Dodger escaped 72% (target about 45%). Pings every 180 s instead of
270 s plus a third extra Bounty Hunter only brought that to 68%. The bots still chased
within `sight_radius_m` 100 m while their team saw 200 m. With `sight_radius_m` at 200 m
too, it is 48% (45% by day, 56% at night). A Dodger standing still escapes 0-2%.
Now: `ping_every_s` 180 (20 real seconds), `hunter_extra` 3, `sight_radius_m` 200.
