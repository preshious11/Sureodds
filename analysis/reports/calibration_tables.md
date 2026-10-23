
## Scope: Top + Daily (the products)


### Production-settled only (WITHOUT graded voids) — n = 48 picks on 46 distinct fixtures


#### Calibration (model probability bins)

| Model prob bin | n | mean model prob | mean p_market | actual win rate | gap (actual − model) |
|---|---|---|---|---|---|
| <50 | 0 | | | | |
| 50–55 | 0 | | | | |
| 55–60 | 18 (too small to conclude) | 57.1% | 56.4% | 72.2% | +15.1 pts |
| 60–65 | 18 (too small to conclude) | 62.3% | 62.1% | 55.6% | -6.8 pts |
| 65–70 | 4 (too small to conclude) | 68.0% | 68.7% | 75.0% | +7.0 pts |
| 70–80 | 6 (too small to conclude) | 73.5% | 73.2% | 83.3% | +9.8 pts |
| 80+ | 2 (too small to conclude) | 81.2% | 80.6% | 100.0% | +18.8 pts |


Same bins, binary lines only (no push/half mass, so the published prob IS P(won | won or lost)):

| Model prob bin | n | mean model prob | mean p_market | actual win rate | gap (actual − model) |
|---|---|---|---|---|---|
| <50 | 0 | | | | |
| 50–55 | 0 | | | | |
| 55–60 | 18 (too small to conclude) | 57.1% | 56.4% | 72.2% | +15.1 pts |
| 60–65 | 18 (too small to conclude) | 62.3% | 62.1% | 55.6% | -6.8 pts |
| 65–70 | 3 (too small to conclude) | 68.1% | 68.1% | 66.7% | -1.5 pts |
| 70–80 | 6 (too small to conclude) | 73.5% | 73.2% | 83.3% | +9.8 pts |
| 80+ | 2 (too small to conclude) | 81.2% | 80.6% | 100.0% | +18.8 pts |


#### Brier / log loss — model vs market baseline (same picks)

p_market missing on 0 of 48 picks (excluded from the baseline columns only). market_strict_win missing on 0.

| Segment | n | fixtures | win rate | mean prob | Brier model | Brier p_market | Δ Brier (±SE) | LogLoss model | LogLoss p_market | verdict (raw prob) | Brier model cond. | LL model cond. | Δ Brier cond. (±SE) | verdict (like-for-like) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Overall** | 48 (too small to conclude) | 46 | 68.8% | 63.0% | 0.211 | 0.210 | 0.0011 (±0.0018) | 0.612 | 0.609 | **WORSE (Brier+LL)** | 0.211 | 0.611 | 0.0008 (±0.0017) | **WORSE (Brier+LL)** |
| product: top | 16 (too small to conclude) | 16 | 87.5% | 69.5% | 0.141 | 0.141 | -0.0002 (±0.0015) | 0.460 | 0.461 | not worse | 0.139 | 0.458 | -0.0013 (±0.0008) | not worse |
| product: daily | 35 (too small to conclude) | 35 | 60.0% | 60.4% | 0.249 | 0.247 | 0.0014 (±0.0024) | 0.692 | 0.689 | **WORSE (Brier+LL)** | 0.249 | 0.692 | 0.0014 (±0.0024) | **WORSE (Brier+LL)** |
| sport: football | 33 (too small to conclude) | 31 | 69.7% | 63.9% | 0.200 | 0.201 | -0.0007 (±0.0005) | 0.586 | 0.588 | not worse | 0.200 | 0.586 | -0.0007 (±0.0005) | not worse |
| sport: basketball | 5 (too small to conclude) | 5 | 80.0% | 61.3% | 0.198 | 0.199 | -0.0003 (±0.0008) | 0.588 | 0.589 | not worse | 0.198 | 0.588 | -0.0003 (±0.0008) | not worse |
| sport: baseball | 4 (too small to conclude) | 4 | 50.0% | 60.1% | 0.282 | 0.286 | -0.0042 (±0.0078) | 0.759 | 0.767 | not worse | 0.282 | 0.759 | -0.0042 (±0.0078) | not worse |
| sport: tennis | 4 (too small to conclude) | 4 | 75.0% | 60.8% | 0.252 | 0.252 | 0.0000 (±0.0000) | 0.702 | 0.702 | not worse | 0.252 | 0.702 | 0.0000 (±0.0000) | not worse |
| sport: american_football | 2 (too small to conclude) | 2 | 50.0% | 62.4% | 0.215 | 0.167 | 0.0473 (±0.0286) | 0.617 | 0.519 | **WORSE (Brier+LL)** | 0.206 | 0.598 | 0.0389 (±0.0371) | **WORSE (Brier+LL)** |
| market: result3 | 19 (too small to conclude) | 19 | 78.9% | 65.1% | 0.168 | 0.168 | -0.0008 (±0.0008) | 0.516 | 0.519 | not worse | 0.168 | 0.516 | -0.0008 (±0.0008) | not worse |
| market: result2 | 11 (too small to conclude) | 11 | 81.8% | 61.3% | 0.207 | 0.206 | 0.0015 (±0.0018) | 0.608 | 0.605 | **WORSE (Brier+LL)** | 0.206 | 0.605 | -0.0000 (±0.0004) | **WORSE (LL)** |
| market: total | 10 (too small to conclude) | 10 | 60.0% | 61.2% | 0.228 | 0.223 | 0.0043 (±0.0084) | 0.646 | 0.637 | **WORSE (Brier+LL)** | 0.228 | 0.646 | 0.0043 (±0.0084) | **WORSE (Brier+LL)** |
| market: handicap | 4 (too small to conclude) | 4 | 25.0% | 61.2% | 0.324 | 0.321 | 0.0026 (±0.0017) | 0.845 | 0.839 | **WORSE (Brier+LL)** | 0.324 | 0.845 | 0.0026 (±0.0017) | **WORSE (Brier+LL)** |
| market: btts | 2 (too small to conclude) | 2 | 50.0% | 72.7% | 0.302 | 0.302 | 0.0001 (±0.0009) | 0.810 | 0.810 | **WORSE (Brier+LL)** | 0.302 | 0.810 | 0.0001 (±0.0009) | **WORSE (Brier+LL)** |
| market: dc | 2 (too small to conclude) | 2 | 50.0% | 55.1% | 0.253 | 0.253 | -0.0001 (±0.0007) | 0.698 | 0.699 | not worse | 0.253 | 0.698 | -0.0001 (±0.0007) | not worse |
| quality: main | 41 (too small to conclude) | 41 | 68.3% | 62.2% | 0.212 | 0.210 | 0.0016 (±0.0019) | 0.612 | 0.609 | **WORSE (Brier+LL)** | 0.212 | 0.612 | 0.0016 (±0.0019) | **WORSE (Brier+LL)** |
| quality: secondary | 5 (too small to conclude) | 4 | 60.0% | 69.7% | 0.237 | 0.237 | -0.0006 (±0.0006) | 0.666 | 0.667 | not worse | 0.237 | 0.666 | -0.0006 (±0.0006) | not worse |
| quality: restricted | 2 (too small to conclude) | 2 | 100.0% | 63.1% | 0.139 | 0.143 | -0.0042 (±0.0229) | 0.464 | 0.469 | not worse | 0.130 | 0.444 | -0.0126 (±0.0145) | not worse |
| line can push/half (push mass > 0) | 1 (too small to conclude) | 1 | 100.0% | 67.7% | 0.104 | 0.086 | 0.0187 (±-) | 0.390 | 0.346 | **WORSE (Brier+LL)** | 0.087 | 0.350 | 0.0018 (±-) | **WORSE (Brier+LL)** |
| binary line (no push mass) | 47 (too small to conclude) | 45 | 68.1% | 62.9% | 0.214 | 0.213 | 0.0008 (±0.0018) | 0.616 | 0.615 | **WORSE (Brier+LL)** | 0.214 | 0.616 | 0.0008 (±0.0018) | **WORSE (Brier+LL)** |


#### Flat 1-unit stake result (realised, at the published price)

| Segment | n | realised ROI per pick | mean published EV |
|---|---|---|---|
| **Overall** | 48 (too small to conclude) | +4.9% | -3.4% |
| product: top | 16 (too small to conclude) | +21.8% | -3.3% |
| product: daily | 35 (too small to conclude) | -3.0% | -3.4% |


#### Model vs sharp fair probability (picks with p_fair)

| Subset | n | fixtures | mean (model − p_fair) | mean abs gap | model > sharp on | lean | mean model prob | mean p_fair | actual win rate | Brier model | Brier p_fair | LogLoss model | LogLoss p_fair |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all lines, published prob | 19 (too small to conclude) | 19 | -0.06 pts | 0.07 pts | 5% of picks | LESS confident than sharp | 64.9% | 65.0% | 73.7% | 0.177 | 0.177 | 0.535 | 0.535 |
| all lines, model cond. prob (like-for-like) | 19 (too small to conclude) | 19 | -0.06 pts | 0.07 pts | 5% of picks | LESS confident than sharp | 64.9% | 65.0% | 73.7% | 0.177 | 0.177 | 0.535 | 0.535 |
| binary lines only (no push mass) | 19 (too small to conclude) | 19 | -0.06 pts | 0.07 pts | 5% of picks | LESS confident than sharp | 64.9% | 65.0% | 73.7% | 0.177 | 0.177 | 0.535 | 0.535 |
| push-capable lines, model cond. prob | 0 | | | | | | | | | | | | |


#### Fixture-clustered bootstrap (2,000 resamples of whole fixtures, seed 7) — 90% intervals

Several picks share one match, so picks are not independent; resampling whole fixtures gives honest intervals.

| Quantity | subset | n picks | fixtures | point | 90% interval |
|---|---|---|---|---|---|
| Δ Brier, model cond. − p_market (negative = model better) | all lines | 48 | 46 | +0.0008 | [-0.0016, +0.0041] |
| win rate − mean model cond. prob (positive = model under-confident) | all lines | 48 | 46 | +0.0568 | [-0.0493, +0.1569] |
| Δ Brier, model cond. − p_market (negative = model better) | binary lines | 47 | 45 | +0.0008 | [-0.0016, +0.0041] |
| win rate − mean model cond. prob (positive = model under-confident) | binary lines | 47 | 45 | +0.0517 | [-0.0611, +0.1582] |


### WITH graded voids added (settled + graded_void) — n = 55 picks on 51 distinct fixtures


#### Calibration (model probability bins)

| Model prob bin | n | mean model prob | mean p_market | actual win rate | gap (actual − model) |
|---|---|---|---|---|---|
| <50 | 0 | | | | |
| 50–55 | 0 | | | | |
| 55–60 | 21 (too small to conclude) | 57.1% | 56.4% | 71.4% | +14.3 pts |
| 60–65 | 19 (too small to conclude) | 62.3% | 62.1% | 57.9% | -4.4 pts |
| 65–70 | 5 (too small to conclude) | 67.7% | 68.2% | 80.0% | +12.3 pts |
| 70–80 | 8 (too small to conclude) | 73.0% | 72.7% | 87.5% | +14.5 pts |
| 80+ | 2 (too small to conclude) | 81.2% | 80.6% | 100.0% | +18.8 pts |


Same bins, binary lines only (no push/half mass, so the published prob IS P(won | won or lost)):

| Model prob bin | n | mean model prob | mean p_market | actual win rate | gap (actual − model) |
|---|---|---|---|---|---|
| <50 | 0 | | | | |
| 50–55 | 0 | | | | |
| 55–60 | 21 (too small to conclude) | 57.1% | 56.4% | 71.4% | +14.3 pts |
| 60–65 | 19 (too small to conclude) | 62.3% | 62.1% | 57.9% | -4.4 pts |
| 65–70 | 4 (too small to conclude) | 67.7% | 67.6% | 75.0% | +7.3 pts |
| 70–80 | 8 (too small to conclude) | 73.0% | 72.7% | 87.5% | +14.5 pts |
| 80+ | 2 (too small to conclude) | 81.2% | 80.6% | 100.0% | +18.8 pts |


#### Brier / log loss — model vs market baseline (same picks)

p_market missing on 0 of 55 picks (excluded from the baseline columns only). market_strict_win missing on 0.

| Segment | n | fixtures | win rate | mean prob | Brier model | Brier p_market | Δ Brier (±SE) | LogLoss model | LogLoss p_market | verdict (raw prob) | Brier model cond. | LL model cond. | Δ Brier cond. (±SE) | verdict (like-for-like) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Overall** | 55 | 51 | 70.9% | 63.0% | 0.204 | 0.203 | 0.0011 (±0.0016) | 0.597 | 0.595 | **WORSE (Brier+LL)** | 0.204 | 0.596 | 0.0008 (±0.0015) | **WORSE (Brier+LL)** |
| product: top | 18 (too small to conclude) | 18 | 88.9% | 69.4% | 0.136 | 0.136 | -0.0004 (±0.0013) | 0.451 | 0.452 | not worse | 0.135 | 0.449 | -0.0014 (±0.0007) | not worse |
| product: daily | 40 (too small to conclude) | 40 | 62.5% | 60.4% | 0.240 | 0.238 | 0.0014 (±0.0021) | 0.673 | 0.670 | **WORSE (Brier+LL)** | 0.240 | 0.673 | 0.0014 (±0.0021) | **WORSE (Brier+LL)** |
| sport: football | 38 (too small to conclude) | 34 | 71.1% | 63.7% | 0.195 | 0.196 | -0.0004 (±0.0005) | 0.577 | 0.578 | not worse | 0.195 | 0.577 | -0.0004 (±0.0005) | not worse |
| sport: basketball | 6 (too small to conclude) | 6 | 83.3% | 63.2% | 0.178 | 0.178 | -0.0004 (±0.0006) | 0.544 | 0.544 | not worse | 0.178 | 0.544 | -0.0004 (±0.0006) | not worse |
| sport: baseball | 5 (too small to conclude) | 5 | 60.0% | 59.7% | 0.260 | 0.264 | -0.0035 (±0.0061) | 0.716 | 0.723 | not worse | 0.260 | 0.716 | -0.0035 (±0.0061) | not worse |
| sport: tennis | 4 (too small to conclude) | 4 | 75.0% | 60.8% | 0.252 | 0.252 | 0.0000 (±0.0000) | 0.702 | 0.702 | not worse | 0.252 | 0.702 | 0.0000 (±0.0000) | not worse |
| sport: american_football | 2 (too small to conclude) | 2 | 50.0% | 62.4% | 0.215 | 0.167 | 0.0473 (±0.0286) | 0.617 | 0.519 | **WORSE (Brier+LL)** | 0.206 | 0.598 | 0.0389 (±0.0371) | **WORSE (Brier+LL)** |
| market: result3 | 21 (too small to conclude) | 21 | 81.0% | 65.0% | 0.164 | 0.165 | -0.0009 (±0.0008) | 0.510 | 0.513 | not worse | 0.164 | 0.510 | -0.0009 (±0.0008) | not worse |
| market: total | 13 (too small to conclude) | 12 | 61.5% | 61.2% | 0.219 | 0.214 | 0.0041 (±0.0064) | 0.627 | 0.619 | **WORSE (Brier+LL)** | 0.219 | 0.627 | 0.0041 (±0.0064) | **WORSE (Brier+LL)** |
| market: result2 | 12 (too small to conclude) | 12 | 83.3% | 62.3% | 0.196 | 0.195 | 0.0013 (±0.0016) | 0.584 | 0.581 | **WORSE (Brier+LL)** | 0.195 | 0.581 | -0.0001 (±0.0003) | not worse |
| market: handicap | 5 (too small to conclude) | 5 | 40.0% | 60.6% | 0.294 | 0.292 | 0.0019 (±0.0015) | 0.784 | 0.780 | **WORSE (Brier+LL)** | 0.294 | 0.784 | 0.0019 (±0.0015) | **WORSE (Brier+LL)** |
| market: btts | 2 (too small to conclude) | 2 | 50.0% | 72.7% | 0.302 | 0.302 | 0.0001 (±0.0009) | 0.810 | 0.810 | **WORSE (Brier+LL)** | 0.302 | 0.810 | 0.0001 (±0.0009) | **WORSE (Brier+LL)** |
| market: dc | 2 (too small to conclude) | 2 | 50.0% | 55.1% | 0.253 | 0.253 | -0.0001 (±0.0007) | 0.698 | 0.699 | not worse | 0.253 | 0.698 | -0.0001 (±0.0007) | not worse |
| quality: main | 47 (too small to conclude) | 46 | 70.2% | 62.3% | 0.205 | 0.204 | 0.0015 (±0.0017) | 0.599 | 0.596 | **WORSE (Brier+LL)** | 0.205 | 0.599 | 0.0015 (±0.0017) | **WORSE (Brier+LL)** |
| quality: secondary | 6 (too small to conclude) | 5 | 66.7% | 69.2% | 0.216 | 0.217 | -0.0005 (±0.0005) | 0.624 | 0.625 | not worse | 0.216 | 0.624 | -0.0005 (±0.0005) | not worse |
| quality: restricted | 2 (too small to conclude) | 2 | 100.0% | 63.1% | 0.139 | 0.143 | -0.0042 (±0.0229) | 0.464 | 0.469 | not worse | 0.130 | 0.444 | -0.0126 (±0.0145) | not worse |
| line can push/half (push mass > 0) | 1 (too small to conclude) | 1 | 100.0% | 67.7% | 0.104 | 0.086 | 0.0187 (±-) | 0.390 | 0.346 | **WORSE (Brier+LL)** | 0.087 | 0.350 | 0.0018 (±-) | **WORSE (Brier+LL)** |
| binary line (no push mass) | 54 | 50 | 70.4% | 63.0% | 0.206 | 0.205 | 0.0007 (±0.0016) | 0.600 | 0.599 | **WORSE (Brier+LL)** | 0.206 | 0.600 | 0.0007 (±0.0016) | **WORSE (Brier+LL)** |


#### Flat 1-unit stake result (realised, at the published price)

| Segment | n | realised ROI per pick | mean published EV |
|---|---|---|---|
| **Overall** | 55 | +8.0% | -3.4% |
| product: top | 18 (too small to conclude) | +24.0% | -3.3% |
| product: daily | 40 (too small to conclude) | +0.4% | -3.4% |


#### Model vs sharp fair probability (picks with p_fair)

| Subset | n | fixtures | mean (model − p_fair) | mean abs gap | model > sharp on | lean | mean model prob | mean p_fair | actual win rate | Brier model | Brier p_fair | LogLoss model | LogLoss p_fair |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all lines, published prob | 22 (too small to conclude) | 22 | -0.06 pts | 0.06 pts | 5% of picks | LESS confident than sharp | 64.4% | 64.4% | 72.7% | 0.179 | 0.179 | 0.540 | 0.539 |
| all lines, model cond. prob (like-for-like) | 22 (too small to conclude) | 22 | -0.06 pts | 0.06 pts | 5% of picks | LESS confident than sharp | 64.4% | 64.4% | 72.7% | 0.179 | 0.179 | 0.540 | 0.539 |
| binary lines only (no push mass) | 22 (too small to conclude) | 22 | -0.06 pts | 0.06 pts | 5% of picks | LESS confident than sharp | 64.4% | 64.4% | 72.7% | 0.179 | 0.179 | 0.540 | 0.539 |
| push-capable lines, model cond. prob | 0 | | | | | | | | | | | | |


#### Fixture-clustered bootstrap (2,000 resamples of whole fixtures, seed 7) — 90% intervals

Several picks share one match, so picks are not independent; resampling whole fixtures gives honest intervals.

| Quantity | subset | n picks | fixtures | point | 90% interval |
|---|---|---|---|---|---|
| Δ Brier, model cond. − p_market (negative = model better) | all lines | 55 | 51 | +0.0008 | [-0.0014, +0.0035] |
| win rate − mean model cond. prob (positive = model under-confident) | all lines | 55 | 51 | +0.0782 | [-0.0197, +0.1736] |
| Δ Brier, model cond. − p_market (negative = model better) | binary lines | 54 | 50 | +0.0007 | [-0.0014, +0.0037] |
| win rate − mean model cond. prob (positive = model under-confident) | binary lines | 54 | 50 | +0.0741 | [-0.0192, +0.1731] |


### Graded voids only (for reference) — n = 7 picks on 5 distinct fixtures


#### Calibration (model probability bins)

| Model prob bin | n | mean model prob | mean p_market | actual win rate | gap (actual − model) |
|---|---|---|---|---|---|
| <50 | 0 | | | | |
| 50–55 | 0 | | | | |
| 55–60 | 3 (too small to conclude) | 57.0% | 56.6% | 66.7% | +9.7 pts |
| 60–65 | 1 (too small to conclude) | 62.1% | 62.0% | 100.0% | +37.9 pts |
| 65–70 | 1 (too small to conclude) | 66.4% | 66.3% | 100.0% | +33.6 pts |
| 70–80 | 2 (too small to conclude) | 71.6% | 71.2% | 100.0% | +28.4 pts |
| 80+ | 0 | | | | |


Same bins, binary lines only (no push/half mass, so the published prob IS P(won | won or lost)):

| Model prob bin | n | mean model prob | mean p_market | actual win rate | gap (actual − model) |
|---|---|---|---|---|---|
| <50 | 0 | | | | |
| 50–55 | 0 | | | | |
| 55–60 | 3 (too small to conclude) | 57.0% | 56.6% | 66.7% | +9.7 pts |
| 60–65 | 1 (too small to conclude) | 62.1% | 62.0% | 100.0% | +37.9 pts |
| 65–70 | 1 (too small to conclude) | 66.4% | 66.3% | 100.0% | +33.6 pts |
| 70–80 | 2 (too small to conclude) | 71.6% | 71.2% | 100.0% | +28.4 pts |
| 80+ | 0 | | | | |


#### Brier / log loss — model vs market baseline (same picks)

p_market missing on 0 of 7 picks (excluded from the baseline columns only). market_strict_win missing on 0.

| Segment | n | fixtures | win rate | mean prob | Brier model | Brier p_market | Δ Brier (±SE) | LogLoss model | LogLoss p_market | verdict (raw prob) | Brier model cond. | LL model cond. | Δ Brier cond. (±SE) | verdict (like-for-like) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Overall** | 7 (too small to conclude) | 5 | 85.7% | 63.2% | 0.155 | 0.154 | 0.0007 (±0.0017) | 0.494 | 0.493 | **WORSE (Brier+LL)** | 0.155 | 0.494 | 0.0007 (±0.0017) | **WORSE (Brier+LL)** |
| product: top | 2 (too small to conclude) | 2 | 100.0% | 68.5% | 0.100 | 0.102 | -0.0018 (±0.0017) | 0.379 | 0.383 | not worse | 0.100 | 0.379 | -0.0018 (±0.0017) | not worse |
| product: daily | 5 (too small to conclude) | 5 | 80.0% | 61.1% | 0.177 | 0.175 | 0.0017 (±0.0022) | 0.541 | 0.537 | **WORSE (Brier+LL)** | 0.177 | 0.541 | 0.0017 (±0.0022) | **WORSE (Brier+LL)** |
| sport: football | 5 (too small to conclude) | 3 | 80.0% | 62.4% | 0.166 | 0.165 | 0.0013 (±0.0024) | 0.519 | 0.517 | **WORSE (Brier+LL)** | 0.166 | 0.519 | 0.0013 (±0.0024) | **WORSE (Brier+LL)** |
| sport: baseball | 1 (too small to conclude) | 1 | 100.0% | 58.1% | 0.175 | 0.176 | -0.0009 (±-) | 0.543 | 0.545 | not worse | 0.175 | 0.543 | -0.0009 (±-) | not worse |
| sport: basketball | 1 (too small to conclude) | 1 | 100.0% | 72.6% | 0.075 | 0.076 | -0.0007 (±-) | 0.321 | 0.322 | not worse | 0.075 | 0.321 | -0.0007 (±-) | not worse |
| market: total | 3 (too small to conclude) | 2 | 66.7% | 61.3% | 0.188 | 0.185 | 0.0034 (±0.0035) | 0.565 | 0.558 | **WORSE (Brier+LL)** | 0.188 | 0.565 | 0.0034 (±0.0035) | **WORSE (Brier+LL)** |
| market: result3 | 2 (too small to conclude) | 2 | 100.0% | 64.0% | 0.134 | 0.136 | -0.0019 (±0.0016) | 0.451 | 0.456 | not worse | 0.134 | 0.451 | -0.0019 (±0.0016) | not worse |
| market: handicap | 1 (too small to conclude) | 1 | 100.0% | 58.1% | 0.175 | 0.176 | -0.0009 (±-) | 0.543 | 0.545 | not worse | 0.175 | 0.543 | -0.0009 (±-) | not worse |
| market: result2 | 1 (too small to conclude) | 1 | 100.0% | 72.6% | 0.075 | 0.076 | -0.0007 (±-) | 0.321 | 0.322 | not worse | 0.075 | 0.321 | -0.0007 (±-) | not worse |
| quality: main | 6 (too small to conclude) | 5 | 83.3% | 62.7% | 0.162 | 0.161 | 0.0008 (±0.0020) | 0.508 | 0.507 | **WORSE (Brier+LL)** | 0.162 | 0.508 | 0.0008 (±0.0020) | **WORSE (Brier+LL)** |
| quality: secondary | 1 (too small to conclude) | 1 | 100.0% | 66.4% | 0.113 | 0.113 | -0.0001 (±-) | 0.410 | 0.410 | not worse | 0.113 | 0.410 | -0.0001 (±-) | not worse |
| line can push/half (push mass > 0) | 0 | | | | | | | | | | | | | |
| binary line (no push mass) | 7 (too small to conclude) | 5 | 85.7% | 63.2% | 0.155 | 0.154 | 0.0007 (±0.0017) | 0.494 | 0.493 | **WORSE (Brier+LL)** | 0.155 | 0.494 | 0.0007 (±0.0017) | **WORSE (Brier+LL)** |


#### Flat 1-unit stake result (realised, at the published price)

| Segment | n | realised ROI per pick | mean published EV |
|---|---|---|---|
| **Overall** | 7 (too small to conclude) | +29.3% | -3.4% |
| product: top | 2 (too small to conclude) | +41.5% | -3.2% |
| product: daily | 5 (too small to conclude) | +24.5% | -3.4% |


#### Model vs sharp fair probability (picks with p_fair)

| Subset | n | fixtures | mean (model − p_fair) | mean abs gap | model > sharp on | lean | mean model prob | mean p_fair | actual win rate | Brier model | Brier p_fair | LogLoss model | LogLoss p_fair |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all lines, published prob | 3 (too small to conclude) | 3 | -0.04 pts | 0.04 pts | 0% of picks | LESS confident than sharp | 61.1% | 61.2% | 66.7% | 0.191 | 0.191 | 0.570 | 0.570 |
| all lines, model cond. prob (like-for-like) | 3 (too small to conclude) | 3 | -0.04 pts | 0.04 pts | 0% of picks | LESS confident than sharp | 61.1% | 61.2% | 66.7% | 0.191 | 0.191 | 0.570 | 0.570 |
| binary lines only (no push mass) | 3 (too small to conclude) | 3 | -0.04 pts | 0.04 pts | 0% of picks | LESS confident than sharp | 61.1% | 61.2% | 66.7% | 0.191 | 0.191 | 0.570 | 0.570 |
| push-capable lines, model cond. prob | 0 | | | | | | | | | | | | |


#### Fixture-clustered bootstrap (2,000 resamples of whole fixtures, seed 7) — 90% intervals

Several picks share one match, so picks are not independent; resampling whole fixtures gives honest intervals.

| Quantity | subset | n picks | fixtures | point | 90% interval |
|---|---|---|---|---|---|
| Δ Brier, model cond. − p_market (negative = model better) | all lines | 7 | 5 | +0.0007 | [-0.0014, +0.0027] |
| win rate − mean model cond. prob (positive = model under-confident) | all lines | 7 | 5 | +0.2250 | [+0.0694, +0.3928] |
| Δ Brier, model cond. − p_market (negative = model better) | binary lines | 7 | 5 | +0.0007 | [-0.0014, +0.0027] |
| win rate − mean model cond. prob (positive = model under-confident) | binary lines | 7 | 5 | +0.2250 | [+0.0694, +0.3928] |


## Scope: All published predictions (any membership, incl. the 'all' collection) - SUPPLEMENTARY


### Production-settled only (WITHOUT graded voids) — n = 78 picks on 46 distinct fixtures


#### Calibration (model probability bins)

| Model prob bin | n | mean model prob | mean p_market | actual win rate | gap (actual − model) |
|---|---|---|---|---|---|
| <50 | 9 (too small to conclude) | 40.4% | 50.8% | 77.8% | +37.4 pts |
| 50–55 | 7 (too small to conclude) | 51.6% | 51.6% | 28.6% | -23.0 pts |
| 55–60 | 20 (too small to conclude) | 57.3% | 56.7% | 70.0% | +12.7 pts |
| 60–65 | 20 (too small to conclude) | 62.2% | 62.8% | 60.0% | -2.2 pts |
| 65–70 | 6 (too small to conclude) | 67.7% | 71.1% | 66.7% | -1.0 pts |
| 70–80 | 9 (too small to conclude) | 73.8% | 75.0% | 88.9% | +15.1 pts |
| 80+ | 7 (too small to conclude) | 84.2% | 85.6% | 100.0% | +15.8 pts |


Same bins, binary lines only (no push/half mass, so the published prob IS P(won | won or lost)):

| Model prob bin | n | mean model prob | mean p_market | actual win rate | gap (actual − model) |
|---|---|---|---|---|---|
| <50 | 2 (too small to conclude) | 41.7% | 41.7% | 100.0% | +58.3 pts |
| 50–55 | 7 (too small to conclude) | 51.6% | 51.6% | 28.6% | -23.0 pts |
| 55–60 | 20 (too small to conclude) | 57.3% | 56.7% | 70.0% | +12.7 pts |
| 60–65 | 19 (too small to conclude) | 62.3% | 62.1% | 57.9% | -4.4 pts |
| 65–70 | 4 (too small to conclude) | 67.6% | 67.6% | 50.0% | -17.6 pts |
| 70–80 | 8 (too small to conclude) | 73.2% | 72.9% | 87.5% | +14.3 pts |
| 80+ | 6 (too small to conclude) | 84.8% | 84.4% | 100.0% | +15.2 pts |


#### Brier / log loss — model vs market baseline (same picks)

p_market missing on 0 of 78 picks (excluded from the baseline columns only). market_strict_win missing on 0.

| Segment | n | fixtures | win rate | mean prob | Brier model | Brier p_market | Δ Brier (±SE) | LogLoss model | LogLoss p_market | verdict (raw prob) | Brier model cond. | LL model cond. | Δ Brier cond. (±SE) | verdict (like-for-like) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Overall** | 78 | 46 | 69.2% | 61.2% | 0.208 | 0.200 | 0.0083 (±0.0050) | 0.600 | 0.581 | **WORSE (Brier+LL)** | 0.198 | 0.577 | -0.0016 (±0.0018) | not worse |
| product: top | 16 (too small to conclude) | 16 | 87.5% | 69.5% | 0.141 | 0.141 | -0.0002 (±0.0015) | 0.460 | 0.461 | not worse | 0.139 | 0.458 | -0.0013 (±0.0008) | not worse |
| product: daily | 35 (too small to conclude) | 35 | 60.0% | 60.4% | 0.249 | 0.247 | 0.0014 (±0.0024) | 0.692 | 0.689 | **WORSE (Brier+LL)** | 0.249 | 0.692 | 0.0014 (±0.0024) | **WORSE (Brier+LL)** |
| product: all-only (not top/daily) | 30 (too small to conclude) | 7 | 70.0% | 58.3% | 0.203 | 0.183 | 0.0198 (±0.0124) | 0.582 | 0.535 | **WORSE (Brier+LL)** | 0.178 | 0.523 | -0.0055 (±0.0036) | not worse |
| sport: football | 60 | 31 | 71.7% | 61.5% | 0.198 | 0.189 | 0.0095 (±0.0063) | 0.578 | 0.555 | **WORSE (Brier+LL)** | 0.186 | 0.549 | -0.0031 (±0.0018) | not worse |
| sport: baseball | 7 (too small to conclude) | 4 | 42.9% | 58.6% | 0.271 | 0.274 | -0.0024 (±0.0042) | 0.737 | 0.742 | not worse | 0.271 | 0.737 | -0.0024 (±0.0042) | not worse |
| sport: basketball | 5 (too small to conclude) | 5 | 80.0% | 61.3% | 0.198 | 0.199 | -0.0003 (±0.0008) | 0.588 | 0.589 | not worse | 0.198 | 0.588 | -0.0003 (±0.0008) | not worse |
| sport: tennis | 4 (too small to conclude) | 4 | 75.0% | 60.8% | 0.252 | 0.252 | 0.0000 (±0.0000) | 0.702 | 0.702 | not worse | 0.252 | 0.702 | 0.0000 (±0.0000) | not worse |
| sport: american_football | 2 (too small to conclude) | 2 | 50.0% | 62.4% | 0.215 | 0.167 | 0.0473 (±0.0286) | 0.617 | 0.519 | **WORSE (Brier+LL)** | 0.206 | 0.598 | 0.0389 (±0.0371) | **WORSE (Brier+LL)** |
| market: result3 | 21 (too small to conclude) | 21 | 81.0% | 62.9% | 0.184 | 0.185 | -0.0008 (±0.0007) | 0.551 | 0.553 | not worse | 0.184 | 0.551 | -0.0008 (±0.0007) | not worse |
| market: total | 15 (too small to conclude) | 13 | 40.0% | 55.8% | 0.222 | 0.237 | -0.0157 (±0.0145) | 0.633 | 0.666 | not worse | 0.234 | 0.659 | -0.0033 (±0.0081) | not worse |
| market: result2 | 12 (too small to conclude) | 12 | 75.0% | 61.2% | 0.220 | 0.218 | 0.0015 (±0.0016) | 0.633 | 0.629 | **WORSE (Brier+LL)** | 0.218 | 0.629 | 0.0001 (±0.0004) | **WORSE (Brier+LL)** |
| market: btts | 8 (too small to conclude) | 8 | 50.0% | 59.9% | 0.274 | 0.274 | -0.0001 (±0.0004) | 0.745 | 0.745 | not worse | 0.274 | 0.745 | -0.0001 (±0.0004) | not worse |
| market: dc | 8 (too small to conclude) | 8 | 87.5% | 75.1% | 0.092 | 0.093 | -0.0004 (±0.0002) | 0.329 | 0.330 | not worse | 0.092 | 0.329 | -0.0004 (±0.0002) | not worse |
| market: handicap | 8 (too small to conclude) | 8 | 62.5% | 54.0% | 0.307 | 0.268 | 0.0385 (±0.0192) | 0.809 | 0.731 | **WORSE (Brier+LL)** | 0.261 | 0.716 | -0.0072 (±0.0087) | not worse |
| market: dnb | 6 (too small to conclude) | 6 | 100.0% | 61.8% | 0.171 | 0.075 | 0.0966 (±0.0240) | 0.518 | 0.282 | **WORSE (Brier+LL)** | 0.075 | 0.282 | 0.0001 (±0.0000) | **WORSE (Brier+LL)** |
| quality: main | 65 | 42 | 70.8% | 61.0% | 0.203 | 0.193 | 0.0102 (±0.0059) | 0.588 | 0.564 | **WORSE (Brier+LL)** | 0.191 | 0.561 | -0.0015 (±0.0021) | not worse |
| quality: secondary | 11 (too small to conclude) | 10 | 54.5% | 62.0% | 0.252 | 0.252 | -0.0003 (±0.0004) | 0.697 | 0.698 | not worse | 0.252 | 0.697 | -0.0003 (±0.0004) | not worse |
| quality: restricted | 2 (too small to conclude) | 2 | 100.0% | 63.1% | 0.139 | 0.143 | -0.0042 (±0.0229) | 0.464 | 0.469 | not worse | 0.130 | 0.444 | -0.0126 (±0.0145) | not worse |
| line can push/half (push mass > 0) | 12 (too small to conclude) | 7 | 83.3% | 53.0% | 0.198 | 0.147 | 0.0515 (±0.0295) | 0.578 | 0.454 | **WORSE (Brier+LL)** | 0.134 | 0.428 | -0.0131 (±0.0087) | not worse |
| binary line (no push mass) | 66 | 45 | 66.7% | 62.7% | 0.210 | 0.209 | 0.0005 (±0.0013) | 0.604 | 0.604 | **WORSE (Brier+LL)** | 0.210 | 0.604 | 0.0005 (±0.0013) | **WORSE (Brier+LL)** |


#### Flat 1-unit stake result (realised, at the published price)

| Segment | n | realised ROI per pick | mean published EV |
|---|---|---|---|
| **Overall** | 78 | +5.2% | -3.9% |
| product: top | 16 (too small to conclude) | +21.8% | -3.3% |
| product: daily | 35 (too small to conclude) | -3.0% | -3.4% |
| product: all-only (not top/daily) | 30 (too small to conclude) | +5.7% | -4.6% |


#### Model vs sharp fair probability (picks with p_fair)

| Subset | n | fixtures | mean (model − p_fair) | mean abs gap | model > sharp on | lean | mean model prob | mean p_fair | actual win rate | Brier model | Brier p_fair | LogLoss model | LogLoss p_fair |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all lines, published prob | 27 (too small to conclude) | 19 | -2.28 pts | 2.28 pts | 7% of picks | LESS confident than sharp | 58.5% | 60.7% | 66.7% | 0.202 | 0.201 | 0.589 | 0.587 |
| all lines, model cond. prob (like-for-like) | 27 (too small to conclude) | 19 | -0.11 pts | 0.67 pts | 22% of picks | LESS confident than sharp | 60.6% | 60.7% | 66.7% | 0.195 | 0.201 | 0.576 | 0.587 |
| binary lines only (no push mass) | 22 (too small to conclude) | 19 | -0.05 pts | 0.06 pts | 9% of picks | LESS confident than sharp | 62.8% | 62.8% | 68.2% | 0.191 | 0.191 | 0.566 | 0.566 |
| push-capable lines, model cond. prob | 5 (too small to conclude) | 4 | -0.35 pts | 3.37 pts | 80% of picks | LESS confident than sharp | 51.2% | 51.5% | 60.0% | 0.213 | 0.244 | 0.619 | 0.681 |


#### Fixture-clustered bootstrap (2,000 resamples of whole fixtures, seed 7) — 90% intervals

Several picks share one match, so picks are not independent; resampling whole fixtures gives honest intervals.

| Quantity | subset | n picks | fixtures | point | 90% interval |
|---|---|---|---|---|---|
| Δ Brier, model cond. − p_market (negative = model better) | all lines | 78 | 46 | -0.0016 | [-0.0041, +0.0014] |
| win rate − mean model cond. prob (positive = model under-confident) | all lines | 78 | 46 | +0.0606 | [-0.0215, +0.1359] |
| Δ Brier, model cond. − p_market (negative = model better) | binary lines | 66 | 45 | +0.0005 | [-0.0012, +0.0029] |
| win rate − mean model cond. prob (positive = model under-confident) | binary lines | 66 | 45 | +0.0396 | [-0.0580, +0.1306] |


### WITH graded voids added (settled + graded_void) — n = 233 picks on 83 distinct fixtures


#### Calibration (model probability bins)

| Model prob bin | n | mean model prob | mean p_market | actual win rate | gap (actual − model) |
|---|---|---|---|---|---|
| <50 | 47 (too small to conclude) | 42.8% | 50.5% | 51.1% | +8.3 pts |
| 50–55 | 49 (too small to conclude) | 51.5% | 51.8% | 57.1% | +5.6 pts |
| 55–60 | 37 (too small to conclude) | 57.2% | 58.8% | 64.9% | +7.6 pts |
| 60–65 | 27 (too small to conclude) | 62.0% | 63.0% | 63.0% | +1.0 pts |
| 65–70 | 20 (too small to conclude) | 67.5% | 69.3% | 65.0% | -2.5 pts |
| 70–80 | 43 (too small to conclude) | 74.4% | 75.2% | 83.7% | +9.3 pts |
| 80+ | 10 (too small to conclude) | 85.5% | 86.4% | 100.0% | +14.5 pts |


Same bins, binary lines only (no push/half mass, so the published prob IS P(won | won or lost)):

| Model prob bin | n | mean model prob | mean p_market | actual win rate | gap (actual − model) |
|---|---|---|---|---|---|
| <50 | 18 (too small to conclude) | 43.8% | 43.9% | 38.9% | -4.9 pts |
| 50–55 | 48 (too small to conclude) | 51.5% | 51.5% | 56.2% | +4.7 pts |
| 55–60 | 33 (too small to conclude) | 57.3% | 56.9% | 63.6% | +6.3 pts |
| 60–65 | 25 (too small to conclude) | 62.1% | 61.9% | 60.0% | -2.1 pts |
| 65–70 | 17 (too small to conclude) | 67.4% | 67.3% | 58.8% | -8.5 pts |
| 70–80 | 40 (too small to conclude) | 74.3% | 74.2% | 82.5% | +8.2 pts |
| 80+ | 9 (too small to conclude) | 86.0% | 85.7% | 100.0% | +14.0 pts |


#### Brier / log loss — model vs market baseline (same picks)

p_market missing on 0 of 233 picks (excluded from the baseline columns only). market_strict_win missing on 0.

| Segment | n | fixtures | win rate | mean prob | Brier model | Brier p_market | Δ Brier (±SE) | LogLoss model | LogLoss p_market | verdict (raw prob) | Brier model cond. | LL model cond. | Δ Brier cond. (±SE) | verdict (like-for-like) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Overall** | 233 | 83 | 65.2% | 58.9% | 0.214 | 0.208 | 0.0061 (±0.0034) | 0.614 | 0.600 | **WORSE (Brier+LL)** | 0.206 | 0.597 | -0.0014 (±0.0009) | not worse |
| product: top | 18 (too small to conclude) | 18 | 88.9% | 69.4% | 0.136 | 0.136 | -0.0004 (±0.0013) | 0.451 | 0.452 | not worse | 0.135 | 0.449 | -0.0014 (±0.0007) | not worse |
| product: daily | 40 (too small to conclude) | 40 | 62.5% | 60.4% | 0.240 | 0.238 | 0.0014 (±0.0021) | 0.673 | 0.670 | **WORSE (Brier+LL)** | 0.240 | 0.673 | 0.0014 (±0.0021) | **WORSE (Brier+LL)** |
| product: all-only (not top/daily) | 178 | 43 | 63.5% | 57.7% | 0.217 | 0.209 | 0.0076 (±0.0044) | 0.620 | 0.601 | **WORSE (Brier+LL)** | 0.207 | 0.597 | -0.0021 (±0.0011) | not worse |
| sport: football | 187 | 56 | 67.9% | 59.3% | 0.208 | 0.201 | 0.0072 (±0.0042) | 0.601 | 0.584 | **WORSE (Brier+LL)** | 0.199 | 0.579 | -0.0021 (±0.0010) | not worse |
| sport: basketball | 28 (too small to conclude) | 14 | 50.0% | 57.2% | 0.230 | 0.230 | 0.0000 (±0.0002) | 0.651 | 0.651 | **WORSE (Brier+LL)** | 0.230 | 0.651 | 0.0000 (±0.0002) | **WORSE (Brier+LL)** |
| sport: baseball | 8 (too small to conclude) | 5 | 50.0% | 58.5% | 0.259 | 0.262 | -0.0022 (±0.0037) | 0.713 | 0.718 | not worse | 0.259 | 0.713 | -0.0022 (±0.0037) | not worse |
| sport: american_football | 6 (too small to conclude) | 4 | 66.7% | 54.2% | 0.235 | 0.222 | 0.0133 (±0.0133) | 0.661 | 0.633 | **WORSE (Brier+LL)** | 0.235 | 0.659 | 0.0130 (±0.0126) | **WORSE (Brier+LL)** |
| sport: tennis | 4 (too small to conclude) | 4 | 75.0% | 60.8% | 0.252 | 0.252 | 0.0000 (±0.0000) | 0.702 | 0.702 | not worse | 0.252 | 0.702 | 0.0000 (±0.0000) | not worse |
| market: total | 51 | 46 | 58.8% | 52.0% | 0.240 | 0.238 | 0.0011 (±0.0077) | 0.671 | 0.669 | **WORSE (Brier+LL)** | 0.235 | 0.662 | -0.0034 (±0.0036) | not worse |
| market: result3 | 46 (too small to conclude) | 46 | 65.2% | 56.2% | 0.195 | 0.196 | -0.0008 (±0.0004) | 0.576 | 0.578 | not worse | 0.195 | 0.576 | -0.0008 (±0.0004) | not worse |
| market: btts | 33 (too small to conclude) | 33 | 54.5% | 62.5% | 0.262 | 0.261 | 0.0004 (±0.0003) | 0.719 | 0.718 | **WORSE (Brier+LL)** | 0.262 | 0.719 | 0.0004 (±0.0003) | **WORSE (Brier+LL)** |
| market: dc | 33 (too small to conclude) | 33 | 84.8% | 76.5% | 0.124 | 0.124 | -0.0002 (±0.0002) | 0.411 | 0.412 | not worse | 0.124 | 0.411 | -0.0002 (±0.0002) | not worse |
| market: dnb | 27 (too small to conclude) | 27 | 70.4% | 53.8% | 0.198 | 0.165 | 0.0332 (±0.0239) | 0.582 | 0.495 | **WORSE (Brier+LL)** | 0.165 | 0.495 | -0.0000 (±0.0001) | not worse |
| market: handicap | 22 (too small to conclude) | 22 | 59.1% | 51.6% | 0.273 | 0.252 | 0.0213 (±0.0092) | 0.740 | 0.697 | **WORSE (Brier+LL)** | 0.246 | 0.686 | -0.0055 (±0.0043) | not worse |
| market: result2 | 21 (too small to conclude) | 21 | 66.7% | 62.8% | 0.217 | 0.216 | 0.0010 (±0.0009) | 0.624 | 0.622 | **WORSE (Brier+LL)** | 0.216 | 0.622 | 0.0002 (±0.0003) | **WORSE (Brier+LL)** |
| quality: main | 194 | 79 | 66.5% | 58.1% | 0.207 | 0.200 | 0.0073 (±0.0041) | 0.600 | 0.582 | **WORSE (Brier+LL)** | 0.198 | 0.579 | -0.0016 (±0.0011) | not worse |
| quality: secondary | 37 (too small to conclude) | 35 | 56.8% | 63.1% | 0.252 | 0.252 | 0.0002 (±0.0003) | 0.698 | 0.698 | **WORSE (Brier+LL)** | 0.252 | 0.698 | 0.0002 (±0.0003) | **WORSE (Brier+LL)** |
| quality: restricted | 2 (too small to conclude) | 2 | 100.0% | 63.1% | 0.139 | 0.143 | -0.0042 (±0.0229) | 0.464 | 0.469 | not worse | 0.130 | 0.444 | -0.0126 (±0.0145) | not worse |
| line can push/half (push mass > 0) | 43 (too small to conclude) | 29 | 69.8% | 49.7% | 0.223 | 0.191 | 0.0319 (±0.0180) | 0.635 | 0.558 | **WORSE (Brier+LL)** | 0.183 | 0.541 | -0.0085 (±0.0042) | not worse |
| binary line (no push mass) | 190 | 82 | 64.2% | 61.0% | 0.212 | 0.212 | 0.0002 (±0.0005) | 0.610 | 0.609 | **WORSE (Brier+LL)** | 0.212 | 0.610 | 0.0002 (±0.0005) | **WORSE (Brier+LL)** |


#### Flat 1-unit stake result (realised, at the published price)

| Segment | n | realised ROI per pick | mean published EV |
|---|---|---|---|
| **Overall** | 233 | +1.0% | -4.3% |
| product: top | 18 (too small to conclude) | +24.0% | -3.3% |
| product: daily | 40 (too small to conclude) | +0.4% | -3.4% |
| product: all-only (not top/daily) | 178 | -1.2% | -4.6% |


#### Model vs sharp fair probability (picks with p_fair)

| Subset | n | fixtures | mean (model − p_fair) | mean abs gap | model > sharp on | lean | mean model prob | mean p_fair | actual win rate | Brier model | Brier p_fair | LogLoss model | LogLoss p_fair |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all lines, published prob | 56 | 32 | -2.62 pts | 2.64 pts | 18% of picks | LESS confident than sharp | 53.3% | 55.9% | 64.3% | 0.221 | 0.214 | 0.630 | 0.616 |
| all lines, model cond. prob (like-for-like) | 56 | 32 | +0.08 pts | 0.98 pts | 32% of picks | MORE confident than sharp | 56.0% | 55.9% | 64.3% | 0.208 | 0.214 | 0.604 | 0.616 |
| binary lines only (no push mass) | 43 (too small to conclude) | 32 | -0.03 pts | 0.05 pts | 23% of picks | LESS confident than sharp | 57.2% | 57.2% | 62.8% | 0.204 | 0.204 | 0.595 | 0.595 |
| push-capable lines, model cond. prob | 13 (too small to conclude) | 11 | +0.47 pts | 4.03 pts | 62% of picks | MORE confident than sharp | 52.0% | 51.5% | 69.2% | 0.219 | 0.246 | 0.631 | 0.684 |


#### Fixture-clustered bootstrap (2,000 resamples of whole fixtures, seed 7) — 90% intervals

Several picks share one match, so picks are not independent; resampling whole fixtures gives honest intervals.

| Quantity | subset | n picks | fixtures | point | 90% interval |
|---|---|---|---|---|---|
| Δ Brier, model cond. − p_market (negative = model better) | all lines | 233 | 83 | -0.0014 | [-0.0027, +0.0000] |
| win rate − mean model cond. prob (positive = model under-confident) | all lines | 233 | 83 | +0.0383 | [-0.0114, +0.0874] |
| Δ Brier, model cond. − p_market (negative = model better) | binary lines | 190 | 82 | +0.0002 | [-0.0004, +0.0011] |
| win rate − mean model cond. prob (positive = model under-confident) | binary lines | 190 | 82 | +0.0317 | [-0.0201, +0.0796] |


### Graded voids only (for reference) — n = 155 picks on 37 distinct fixtures


#### Calibration (model probability bins)

| Model prob bin | n | mean model prob | mean p_market | actual win rate | gap (actual − model) |
|---|---|---|---|---|---|
| <50 | 38 (too small to conclude) | 43.4% | 50.4% | 44.7% | +1.4 pts |
| 50–55 | 42 (too small to conclude) | 51.5% | 51.8% | 61.9% | +10.4 pts |
| 55–60 | 17 (too small to conclude) | 57.1% | 61.2% | 58.8% | +1.7 pts |
| 60–65 | 7 (too small to conclude) | 61.3% | 63.5% | 71.4% | +10.1 pts |
| 65–70 | 14 (too small to conclude) | 67.5% | 68.6% | 64.3% | -3.2 pts |
| 70–80 | 34 (too small to conclude) | 74.6% | 75.3% | 82.4% | +7.7 pts |
| 80+ | 3 (too small to conclude) | 88.4% | 88.3% | 100.0% | +11.6 pts |


Same bins, binary lines only (no push/half mass, so the published prob IS P(won | won or lost)):

| Model prob bin | n | mean model prob | mean p_market | actual win rate | gap (actual − model) |
|---|---|---|---|---|---|
| <50 | 16 (too small to conclude) | 44.1% | 44.1% | 31.2% | -12.8 pts |
| 50–55 | 41 (too small to conclude) | 51.5% | 51.4% | 61.0% | +9.5 pts |
| 55–60 | 13 (too small to conclude) | 57.4% | 57.2% | 53.8% | -3.5 pts |
| 60–65 | 6 (too small to conclude) | 61.5% | 61.4% | 66.7% | +5.2 pts |
| 65–70 | 13 (too small to conclude) | 67.3% | 67.2% | 61.5% | -5.8 pts |
| 70–80 | 32 (too small to conclude) | 74.6% | 74.5% | 81.2% | +6.6 pts |
| 80+ | 3 (too small to conclude) | 88.4% | 88.3% | 100.0% | +11.6 pts |


#### Brier / log loss — model vs market baseline (same picks)

p_market missing on 0 of 155 picks (excluded from the baseline columns only). market_strict_win missing on 0.

| Segment | n | fixtures | win rate | mean prob | Brier model | Brier p_market | Δ Brier (±SE) | LogLoss model | LogLoss p_market | verdict (raw prob) | Brier model cond. | LL model cond. | Δ Brier cond. (±SE) | verdict (like-for-like) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Overall** | 155 | 37 | 63.2% | 57.8% | 0.217 | 0.212 | 0.0049 (±0.0045) | 0.621 | 0.609 | **WORSE (Brier+LL)** | 0.211 | 0.607 | -0.0013 (±0.0010) | not worse |
| product: top | 2 (too small to conclude) | 2 | 100.0% | 68.5% | 0.100 | 0.102 | -0.0018 (±0.0017) | 0.379 | 0.383 | not worse | 0.100 | 0.379 | -0.0018 (±0.0017) | not worse |
| product: daily | 5 (too small to conclude) | 5 | 80.0% | 61.1% | 0.177 | 0.175 | 0.0017 (±0.0022) | 0.541 | 0.537 | **WORSE (Brier+LL)** | 0.177 | 0.541 | 0.0017 (±0.0022) | **WORSE (Brier+LL)** |
| product: all-only (not top/daily) | 148 | 36 | 62.2% | 57.5% | 0.220 | 0.215 | 0.0051 (±0.0047) | 0.627 | 0.615 | **WORSE (Brier+LL)** | 0.213 | 0.612 | -0.0014 (±0.0010) | not worse |
| sport: football | 127 | 25 | 66.1% | 58.3% | 0.212 | 0.206 | 0.0061 (±0.0054) | 0.612 | 0.597 | **WORSE (Brier+LL)** | 0.205 | 0.594 | -0.0016 (±0.0012) | not worse |
| sport: basketball | 23 (too small to conclude) | 9 | 43.5% | 56.3% | 0.237 | 0.237 | 0.0001 (±0.0001) | 0.665 | 0.665 | **WORSE (Brier+LL)** | 0.237 | 0.665 | 0.0001 (±0.0001) | **WORSE (Brier+LL)** |
| sport: american_football | 4 (too small to conclude) | 2 | 75.0% | 50.0% | 0.245 | 0.249 | -0.0037 (±0.0037) | 0.683 | 0.690 | not worse | 0.249 | 0.690 | 0.0000 (±0.0000) | **WORSE (Brier+LL)** |
| sport: baseball | 1 (too small to conclude) | 1 | 100.0% | 58.1% | 0.175 | 0.176 | -0.0009 (±-) | 0.543 | 0.545 | not worse | 0.175 | 0.543 | -0.0009 (±-) | not worse |
| market: total | 36 (too small to conclude) | 33 | 66.7% | 50.4% | 0.247 | 0.239 | 0.0082 (±0.0090) | 0.687 | 0.671 | **WORSE (Brier+LL)** | 0.235 | 0.663 | -0.0035 (±0.0039) | not worse |
| market: btts | 25 (too small to conclude) | 25 | 56.0% | 63.4% | 0.258 | 0.257 | 0.0005 (±0.0003) | 0.710 | 0.709 | **WORSE (Brier+LL)** | 0.258 | 0.710 | 0.0005 (±0.0003) | **WORSE (Brier+LL)** |
| market: dc | 25 (too small to conclude) | 25 | 84.0% | 77.0% | 0.135 | 0.135 | -0.0001 (±0.0002) | 0.437 | 0.438 | not worse | 0.135 | 0.437 | -0.0001 (±0.0002) | not worse |
| market: result3 | 25 (too small to conclude) | 25 | 52.0% | 50.6% | 0.204 | 0.205 | -0.0007 (±0.0003) | 0.598 | 0.599 | not worse | 0.204 | 0.598 | -0.0007 (±0.0003) | not worse |
| market: dnb | 21 (too small to conclude) | 21 | 61.9% | 51.5% | 0.205 | 0.190 | 0.0150 (±0.0290) | 0.600 | 0.556 | **WORSE (Brier+LL)** | 0.190 | 0.556 | -0.0001 (±0.0001) | not worse |
| market: handicap | 14 (too small to conclude) | 14 | 57.1% | 50.3% | 0.254 | 0.242 | 0.0114 (±0.0091) | 0.700 | 0.677 | **WORSE (Brier+LL)** | 0.238 | 0.668 | -0.0046 (±0.0049) | not worse |
| market: result2 | 9 (too small to conclude) | 9 | 55.6% | 64.8% | 0.213 | 0.213 | 0.0003 (±0.0004) | 0.612 | 0.612 | **WORSE (Brier+LL)** | 0.213 | 0.612 | 0.0003 (±0.0004) | **WORSE (Brier+LL)** |
| quality: main | 129 | 37 | 64.3% | 56.7% | 0.210 | 0.204 | 0.0058 (±0.0054) | 0.605 | 0.592 | **WORSE (Brier+LL)** | 0.202 | 0.588 | -0.0016 (±0.0012) | not worse |
| quality: secondary | 26 (too small to conclude) | 25 | 57.7% | 63.5% | 0.252 | 0.252 | 0.0005 (±0.0003) | 0.699 | 0.698 | **WORSE (Brier+LL)** | 0.252 | 0.699 | 0.0005 (±0.0003) | **WORSE (Brier+LL)** |
| line can push/half (push mass > 0) | 31 (too small to conclude) | 22 | 64.5% | 48.4% | 0.233 | 0.209 | 0.0243 (±0.0223) | 0.657 | 0.598 | **WORSE (Brier+LL)** | 0.202 | 0.585 | -0.0067 (±0.0049) | not worse |
| binary line (no push mass) | 124 | 37 | 62.9% | 60.2% | 0.213 | 0.213 | 0.0001 (±0.0002) | 0.612 | 0.612 | **WORSE (Brier+LL)** | 0.213 | 0.612 | 0.0001 (±0.0002) | **WORSE (Brier+LL)** |


#### Flat 1-unit stake result (realised, at the published price)

| Segment | n | realised ROI per pick | mean published EV |
|---|---|---|---|
| **Overall** | 155 | -1.2% | -4.6% |
| product: top | 2 (too small to conclude) | +41.5% | -3.2% |
| product: daily | 5 (too small to conclude) | +24.5% | -3.4% |
| product: all-only (not top/daily) | 148 | -2.6% | -4.6% |


#### Model vs sharp fair probability (picks with p_fair)

| Subset | n | fixtures | mean (model − p_fair) | mean abs gap | model > sharp on | lean | mean model prob | mean p_fair | actual win rate | Brier model | Brier p_fair | LogLoss model | LogLoss p_fair |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all lines, published prob | 29 (too small to conclude) | 13 | -2.95 pts | 2.97 pts | 28% of picks | LESS confident than sharp | 48.4% | 51.4% | 62.1% | 0.238 | 0.225 | 0.668 | 0.642 |
| all lines, model cond. prob (like-for-like) | 29 (too small to conclude) | 13 | +0.26 pts | 1.26 pts | 41% of picks | MORE confident than sharp | 51.7% | 51.4% | 62.1% | 0.219 | 0.225 | 0.629 | 0.642 |
| binary lines only (no push mass) | 21 (too small to conclude) | 13 | -0.01 pts | 0.05 pts | 38% of picks | LESS confident than sharp | 51.3% | 51.3% | 57.1% | 0.217 | 0.217 | 0.626 | 0.626 |
| push-capable lines, model cond. prob | 8 (too small to conclude) | 7 | +0.97 pts | 4.45 pts | 50% of picks | MORE confident than sharp | 52.5% | 51.6% | 75.0% | 0.223 | 0.246 | 0.639 | 0.686 |


#### Fixture-clustered bootstrap (2,000 resamples of whole fixtures, seed 7) — 90% intervals

Several picks share one match, so picks are not independent; resampling whole fixtures gives honest intervals.

| Quantity | subset | n picks | fixtures | point | 90% interval |
|---|---|---|---|---|---|
| Δ Brier, model cond. − p_market (negative = model better) | all lines | 155 | 37 | -0.0013 | [-0.0028, +0.0002] |
| win rate − mean model cond. prob (positive = model under-confident) | all lines | 155 | 37 | +0.0271 | [-0.0385, +0.0893] |
| Δ Brier, model cond. − p_market (negative = model better) | binary lines | 124 | 37 | +0.0001 | [-0.0003, +0.0005] |
| win rate − mean model cond. prob (positive = model under-confident) | binary lines | 124 | 37 | +0.0275 | [-0.0348, +0.0865] |


## Segments where the model scores WORSE than the p_market baseline

Every segment from the tables above with a higher Brier or log loss than p_market. 'raw' = published prob; 'like-for-like' = the model's own P(won | won or lost). Segments under 50 picks are too small to conclude.

| Scope | Group | Segment | n | raw verdict | like-for-like verdict |
|---|---|---|---|---|---|
| td | without_voids | **Overall** | 48 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | without_voids | product: daily | 35 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | without_voids | sport: american_football | 2 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | without_voids | market: result2 | 11 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (LL)** |
| td | without_voids | market: total | 10 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | without_voids | market: handicap | 4 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | without_voids | market: btts | 2 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | without_voids | quality: main | 41 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | without_voids | line can push/half (push mass > 0) | 1 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | without_voids | binary line (no push mass) | 47 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | with_voids | **Overall** | 55 | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | with_voids | product: daily | 40 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | with_voids | sport: american_football | 2 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | with_voids | market: total | 13 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | with_voids | market: result2 | 12 (too small to conclude) | **WORSE (Brier+LL)** | not worse |
| td | with_voids | market: handicap | 5 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | with_voids | market: btts | 2 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | with_voids | quality: main | 47 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | with_voids | line can push/half (push mass > 0) | 1 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | with_voids | binary line (no push mass) | 54 | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | voids_only | **Overall** | 7 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | voids_only | product: daily | 5 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | voids_only | sport: football | 5 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | voids_only | market: total | 3 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | voids_only | quality: main | 6 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| td | voids_only | binary line (no push mass) | 7 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | without_voids | **Overall** | 78 | **WORSE (Brier+LL)** | not worse |
| all | without_voids | product: daily | 35 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | without_voids | product: all-only (not top/daily) | 30 (too small to conclude) | **WORSE (Brier+LL)** | not worse |
| all | without_voids | sport: football | 60 | **WORSE (Brier+LL)** | not worse |
| all | without_voids | sport: american_football | 2 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | without_voids | market: result2 | 12 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | without_voids | market: handicap | 8 (too small to conclude) | **WORSE (Brier+LL)** | not worse |
| all | without_voids | market: dnb | 6 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | without_voids | quality: main | 65 | **WORSE (Brier+LL)** | not worse |
| all | without_voids | line can push/half (push mass > 0) | 12 (too small to conclude) | **WORSE (Brier+LL)** | not worse |
| all | without_voids | binary line (no push mass) | 66 | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | with_voids | **Overall** | 233 | **WORSE (Brier+LL)** | not worse |
| all | with_voids | product: daily | 40 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | with_voids | product: all-only (not top/daily) | 178 | **WORSE (Brier+LL)** | not worse |
| all | with_voids | sport: football | 187 | **WORSE (Brier+LL)** | not worse |
| all | with_voids | sport: basketball | 28 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | with_voids | sport: american_football | 6 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | with_voids | market: total | 51 | **WORSE (Brier+LL)** | not worse |
| all | with_voids | market: btts | 33 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | with_voids | market: dnb | 27 (too small to conclude) | **WORSE (Brier+LL)** | not worse |
| all | with_voids | market: handicap | 22 (too small to conclude) | **WORSE (Brier+LL)** | not worse |
| all | with_voids | market: result2 | 21 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | with_voids | quality: main | 194 | **WORSE (Brier+LL)** | not worse |
| all | with_voids | quality: secondary | 37 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | with_voids | line can push/half (push mass > 0) | 43 (too small to conclude) | **WORSE (Brier+LL)** | not worse |
| all | with_voids | binary line (no push mass) | 190 | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | voids_only | **Overall** | 155 | **WORSE (Brier+LL)** | not worse |
| all | voids_only | product: daily | 5 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | voids_only | product: all-only (not top/daily) | 148 | **WORSE (Brier+LL)** | not worse |
| all | voids_only | sport: football | 127 | **WORSE (Brier+LL)** | not worse |
| all | voids_only | sport: basketball | 23 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | voids_only | sport: american_football | 4 (too small to conclude) | not worse | **WORSE (Brier+LL)** |
| all | voids_only | market: total | 36 (too small to conclude) | **WORSE (Brier+LL)** | not worse |
| all | voids_only | market: btts | 25 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | voids_only | market: dnb | 21 (too small to conclude) | **WORSE (Brier+LL)** | not worse |
| all | voids_only | market: handicap | 14 (too small to conclude) | **WORSE (Brier+LL)** | not worse |
| all | voids_only | market: result2 | 9 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | voids_only | quality: main | 129 | **WORSE (Brier+LL)** | not worse |
| all | voids_only | quality: secondary | 26 (too small to conclude) | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |
| all | voids_only | line can push/half (push mass > 0) | 31 (too small to conclude) | **WORSE (Brier+LL)** | not worse |
| all | voids_only | binary line (no push mass) | 124 | **WORSE (Brier+LL)** | **WORSE (Brier+LL)** |

## Published EV sign (every published pick, any status)

Top/Daily: 2 of 67 picks had a positive model EV at the published price (mean EV -3.4%). All published predictions: 8 of 1375 (mean EV -4.5%).

