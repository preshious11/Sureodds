# Verified local club badges

These files identify the actual clubs in the supplied current ticket. They
are not generated, recolored or substituted with similarly named clubs. Badges
remain club marks; their owners retain their rights. They are included for
fixture identification, without implying club endorsement of Kovqi.

- `sabah-fk.png`: Sabah FK, Azerbaijan, from the opponent listing on Slavia's
  official site: https://www.slavia.cz/upload/club/logo/1788170499_sabahlogo.png
  Cross-check: https://sabahfc.az/ (the current official mark has a pink treatment).
- `borussia-dortmund.png`: Borussia Dortmund, Wikimedia's raster of the mark
  recorded as sourced from bvb.de:
  https://commons.wikimedia.org/wiki/File:Borussia_Dortmund_logo.svg
  Official cross-check:
  https://www.bvb.de/content/dam/bvbweb/en/matches/football/profis/teams/157.svg
- `fenerbahce.png`: Fenerbahce, from the official Slavia opponent listing:
  https://www.slavia.cz/upload/club/logo/1788170632_fenerbahcelogo.png
- `slavia-praha.png`: SK Slavia Praha, from the club's official match listing:
  https://www.slavia.cz/upload/club/logo/SKS-689b370c296957fb5e18d0dc01d60cda84d80ad6.png

## Reusable catalog for future fixtures

`catalog/` now bundles 396 current-season clubs from the top-25-European-league
catalog at https://github.com/luukhopman/football-logos, pinned to revision
`2a3978f0b4730645c205d855a4bda54c161183e9`. `index.json` records each source
URL, club name, league and Git blob hash. The maintenance importer verifies
those hashes before raster optimization; it does not run during generation.
The source catalog is not a claim of endorsement or ownership of club marks.

`overrides.json` retains the individually checked marks listed above.
`reviewed_aliases.json` contains explicit alternate club names. The catalog
builder also records exact source names and unambiguous variants without
standard FC/CF/SK-style tokens. Runtime matching folds accents/case/punctuation,
but never performs approximate matching or silently picks an ambiguous club.

The runtime loader is `predbot/bot/club_badges.py`; the same fixture grid is used
for every team, regardless of crest availability. Unknown names, missing, blank
or corrupt files omit only that crest. The catalog is broad European coverage,
not every club in the world. Nothing is downloaded during daily rendering.

To maintain the bundle, fetch a newer recursive Git tree JSON for the source
repository, then run `tools/build_club_catalog.py --tree <file> --download`
explicitly. Review the resulting source/alias diff before release. Daily
prediction, delivery and settlement jobs never invoke this maintenance tool.
