---
name: variant-availability
description: Availability, price and size are PER-VARIANT fields on commerce PDPs — scope every read to the requested variant and never collapse them to page-level scalars
---

- Availability, price and size are per-variant fields. A page-level scalar is a lossy
  summary of whichever variant happened to render (926, 682, 601, 676, 678).
- Single scalars are lossy by construction: a comma-joined size string keeps the range
  and destroys the size-to-stock relationship (713, 122, 676).
- Every cheap proxy lies: enabled-CTA state, body-text "out of stock", page-level
  discount flags, and default-variant JSON (908, 408, 284, 678, 607, 897).
- When sources conflict, rank by distance from the commerce layer — commerce
  payload/API > server-rendered structured data > rendered DOM > page text/CTA — and
  verify, because the rank inverts per site (716, 705, 954, 284, 146).
- Variant identity rides the URL, including the query string (122, 137, 873, 800, 908,
  311).
- Out-of-stock is real data. Never scrub it, never "fix" it (912, 678, 607).

## Learned: Scope availability/price/size reads to the requested variant, never the page
**Source:** job 926, job 682, job 601, job 676, job 678

Availability, price and size are PER-VARIANT on commerce PDPs; a single page-level
scalar is a lossy summary of whichever variant happened to render. Scope every read to
the variant the URL asks for: SFCC master products leave .availability-msg EMPTY until a
size swatch fires Product-Variation - job 926 clicked size 4 inside an async evaluate
and watched availability flip to 'In Stock', so the shipped row's availability is true
for the clicked size while other sizes stay sold out. Job 682 resolves the
dwvar_*_color param FIRST then filters ProductGroup.hasVariant[] (18 variants = 3
colors x 6 sizes, all 5 fields per variant) instead of reading page-level offers. Job
601 reads invAvailablityJSON_<catentry> (qty/qtyCount/partNumber/size/colour) for true
per-size stock that swatch aria-labels do not carry. Job 676 picks the offers node using
?color=9536 and confirmed 27.0 matched the per-color price. Job 678: 5 JSON-LD offers =
5 sizes, so select by matching the URL's size against offers rather than reading
offers[0].

## Learned: One comma-joined size string destroys the size-to-stock relationship
**Source:** job 713, job 122, job 676, job 954

Pill-style and swatch-style variant UIs tempt you to join every size label into one
string. That ships a range and loses the axis: job 713 emitted size='XXXS, XXS, XS, S,
M, L, XL' with a single product-level availability, so a consumer cannot ask 'is XS in
stock?'; job 122 shipped 'US 6, US 7, ... US 11' the same way and flagged that it must
be split before any per-size availability analysis; job 676 recorded the joined
'XS, S, M, ... 20, 1...' as an explicit DEFECT the writer was mid-fix on. The size
swatch group contains every size FOR THE STYLE - read the selected/aria-pressed button
(or the size query param), not the group's text (676). Scope note: fields can exist only
at PDP depth - all 24 of chloe.com's listing tiles carried zero size elements while the
PDP had '34, 36, 38, 40' (954), so scan every tile for a field before declaring it
PLP-extractable.

## Learned: Every cheap availability proxy lies - button state, body text, page flags
**Source:** job 908, job 408, job 284, job 678, job 607

Do not derive availability from UI affordances or page text. boohoo AU's Add-to-Bag
stayed ENABLED while JSON-LD offers said OutOfStock, and there is no DOM fallback at all
(908). hydroflask.com showed a DOM 'Out of Stock' text hit beside an enabled Add to Cart
while JSON-LD said InStock, and the adjacent 'As low as' price label is not the price
(408) - negative guards (^As low as$, ^Current Price$) keep both out of the record.
Mountain Warehouse's site analysis carried a 'naive out-of-stock rule' that the
analyzer itself had to correct (284). Body-text scanning is the worst offender: a
soft-404 detector that greps for 'sold out' misclassifies live PDPs carrying per-size
copy - the marker scan was removed entirely (678) and job 607's soft-404 marker list
deliberately EXCLUDES 'sold out' because a sold-out product is a real product. If the
signals disagree, emit the documented one and record the conflict; never scrub the
state to make the row look clean.

## Learned: Rank conflicting sources by distance from the commerce layer, then verify
**Source:** job 716, job 705, job 954, job 284, job 146, job 897

When two sources disagree, prefer the one closest to the commerce layer and prove it
with a second read: GoPro's render-layer JSON-LD said InStock while __NEXT_DATA__
props.pageProps.product.saleInfo reported stockLevel 0 / status NOT_AVAILABLE /
LIMIT_REACHED - the commerce payload was right (716). SFCC raw no-JS HTML said InStock
while client JS had flipped the rendered DOM to OutOfStock - a plain fetch is the source
of truth (705). The reverse also happens: chloe.com's listing JSON-LD ItemList said
InStock for all 24 tiles while the DOM badges showed 7 out-of-stock (954), and on sale
items BigCommerce JSON-LD offers.price holds the DEFAULT variant while the DOM shows the
discounted one (284). So the ladder is a tie-break rule, not a blind rank: commerce
payload/API > server-rendered structured data > rendered DOM > page text/CTA, always
cross-checked. Structural traps on the way down: top-level offers can be EMPTY with the
data in hasVariant[].offers (146), and a hasDiscount class on all 32 tiles is not a sale
signal (897).

## Learned: Variant identity lives in the URL, including the query string
**Source:** job 122, job 137, job 873, job 800, job 908, job 311, job 920

The variant selector is often a query param, not path - drop it and every colourway
collapses to the default variant. Free People carries colour as ?color=024 (122);
Nordstrom Rack's origin/breadcrumb/color query string IS the variant identity, so keep
it verbatim or duplicate-looking rows resolve to different colourways (137); Inditex
selects the variant with ?pelement= and variant-level price/availability requires
carrying it into Phase 2 (873); boohoo AU uses ?colour=black (908). The same param is a
DISCOVERY problem: SFCC serves one URL per colourway (dwvar_{sku}_color=NNN&catid=), so
unfiltered discovery inflates item counts with duplicates of one garment - collapse
variant URLs during dedupe, and run the PDP gate before dedupe (800). Identity schemes
can coexist: kotn's seed used ?colour=teak while output rows used slug variants
(...-in-azure) - normalise both schemes before dedupe (311). A variant-suffixed title
only matches sample_values when the variant params are preserved (920).

## Learned: Resolve conflicting availability by a documented precedence, not a guess
**Source:** job 408, job 601, job 358, job 716

When sources conflict, write the precedence down and follow it, or the row is a coin
flip. hydroflask.com: JSON-LD InStock vs a DOM 'Out of Stock' text hit with an enabled
Add to Cart - the writer pinned a rule instead of picking whichever node it read last
(408); the same job's price sat next to an 'As low as' label that needed a negative
guard. WebSphere Commerce PDPs have no JSON-LD: the documented sources are the
PDP-scoped price element (#listPriceId_<id>) and invAvailablityJSON_<catentry> for
per-size stock (601). Kingfisher JSON-LD offers carry CONFLICTING per-fulfilment
availability (Home Delivery InStock, others OutOfStock) - availability must be rolled up
by a fulfilment rule, never taken from offers[0] (358). GoPro shipped in_stock from
JSON-LD while the deeper saleInfo said stockLevel 0 - the tester flagged it NEEDS_FIXES
and the gate was advisory, so a known-conflicting row still shipped (716). Emit the
documented winner, keep the loser in remarks, and treat a flagged conflict as a blocker
rather than a note.

## Learned: Out-of-stock is real data - never scrub it, never 'fix' it
**Source:** job 912, job 678, job 607, job 408

All-OutOfStock output can be genuinely correct. Margiela's pre-order collection marks
every variant size OutOfStock in JSON-LD while the DOM shows preorder tags; the writer
verified Product.offers.availability AND all 7 hasVariant[].offers before accepting it
rather than 'fixing' it (912). Two harvest jobs shipped exactly the opposite failure:
soft-404 detectors scanning body text for 'sold out' would drop live PDPs carrying
per-size sold-out copy, so the marker scan was removed entirely (678) and the marker
list was rebuilt to EXCLUDE 'sold out' - a sold-out product is a real product (607).
The scrub instinct is an availability-anxiety reflex: a row that says out_of_stock with
a real price and a real url is a success; a row that silently drops the state (or emits
in_stock because the button looked clickable) is a defect (408: DOM text + enabled ATC
vs JSON-LD InStock). Emit the state you proved, cite the source that proved it, and let
the consumer decide.
