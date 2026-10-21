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

## NBA / NFL / MLB (`index_nba.json` / `index_nfl.json` / `index_mlb.json`)

Built from ESPN's public team-logo CDN (`a.espncdn.com/i/teamlogos/<league>/500/<abbr>.png`)
via `tools/build_espn_catalog.py --sport {nba,nfl,mlb} --download`. 30 NBA, 32
NFL and 30 MLB teams, keyed by full team name, bare nickname and abbreviation.
Same runtime discipline as football: team marks remain their owners' property,
included for fixture identification only, never implying endorsement; an
unknown/ambiguous name omits the crest rather than guessing. ESPN does not
expose a content hash the way the GitHub tree API does, so each record stores
the downloaded file's own SHA-256 for audit instead of a pre-known value to
verify against.

## Tennis (`index_tennis.json`)

Tennis picks name players, not teams, so there is no per-player crest -
`tennis_badge_file()` in `club_badges.py` matches the TOURNAMENT/TOUR text
instead (e.g. "ATP Paris", "Australian Open"), falling back to the generic
ATP/WTA tour mark when no specific Slam is named. A small, hand-curated set
built by `tools/build_tennis_catalog.py --download`, every source checked
individually for a free license before inclusion (Wikimedia
`imageinfo.extmetadata.LicenseShortName`):

- ATP Tour mark — Public domain, Wikimedia Commons `File:ATP Tour logo.svg`
- WTA Tour mark — Public domain, English Wikipedia `File:WTA Tour logo.png`
- Australian Open — Public domain, Wikimedia Commons `File:Australian Open Logo 2017.svg`
- French Open / Roland-Garros — CC0, Wikimedia Commons `File:RG-Logo.png`
- US Open — Public domain, Wikimedia Commons `File:Usopen-horizontal-logo.svg`

**Wimbledon is deliberately NOT bundled.** Its crossed-rackets mark is not
mirrored on Wikimedia Commons under any free license (unlike the four marks
above) — that absence is the usual sign of a trademark the rights holder
treats as non-free, so nothing was downloaded for it. An "ATP Wimbledon" /
"WTA Wimbledon" league string still gets a crest: it falls back to the
generic ATP/WTA tour badge via the same longest-alias-match rule that
resolves any other unlisted tournament. If a verified free-license Wimbledon
mark is found later, add it to `SOURCES` in `build_tennis_catalog.py` and
rerun with `--download`.
