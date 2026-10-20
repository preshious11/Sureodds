The user supplied `logo-source.png` as the approved Kovqi logo. The renderer
crops and alpha-mattes the existing pixels from this source; it does not
generate a replacement. Black surrounding pixels become transparent at runtime.

Barlow Regular, Barlow SemiBold and Barlow Condensed Bold are bundled under the SIL Open Font
License (see OFL-Barlow.txt and OFL.txt). Sources:

- https://github.com/google/fonts/tree/main/ofl/barlow
- https://github.com/google/fonts/tree/main/ofl/barlowcondensed

`sample-preview.jpg` contains fictional, clearly labeled sample data. It is
never loaded by the publishing code. Production uses the selected structured
ticket and the configured community invitation URL.

`stadium-background.png` is an original decorative asset created for this
project with the built-in imagegen tool. It is bundled locally and is optional:
the renderer uses a local gradient when it is missing or unreadable. No image
generation service or remote image request is used during daily rendering.
The creation prompt is retained in `stadium-background-prompt.txt`. The mark,
all lettering, fixtures, prices, date, CTA and QR are composed deterministically
in Pillow, separately from the generated background.

The current-ticket preview stays local because it contains the configured
private invitation. Repository previews use a fictional community URL.
The four `sample-*` previews are labeled fictional
layout tests. They are never loaded by production publishing.

`badges/` contains a 396-club offline catalog, explicit aliases and the separately
reviewed club marks, with provenance in `badges/SOURCES.md`. Unknown or unavailable
crests are omitted within the same fixture grid. No badge downloads occur during
generation. Kaushan Script supplies the
reference's handwritten slogan and good-luck line under `OFL-Kaushan.txt`; an
unavailable script font falls back to the existing display face.

Anton supplies the heavier reference heading under `OFL-Anton.txt`. The three
`future-ticket-*` files use a later date and other catalog clubs to demonstrate
the reusable template; their fixtures and odds are explicitly sample data.
