# Training Results Snapshot

Run ID: `20260411_213805_c29ee5`
Snapshot taken at: Generation 5 (training still in progress)
Date: 2026-04-11

## Survivors (Gen 5)

### Survivor 0 — Best citation predictor
- **Citation corr: 0.395** | Acceptance corr: 0.430
- Role: `06_significance_and_impact.md`
- Persona: `trendy.json`
- Interests: `efficiency_and_compression.md`
- Methodology: `three_stage_review_budgeted.md`
- Format: `generic.md`

### Survivor 1 — Second citation predictor
- **Citation corr: 0.357** | Acceptance corr: 0.415
- Role: `05_clarity_and_presentation.md`
- Persona: `trendy.json`
- Interests: `efficiency_and_compression.md`
- Methodology: `three_stage_review_budgeted.md`
- Format: `generic.md`

### Survivor 2 — Best acceptance predictor
- Citation corr: 0.121 | **Acceptance corr: 0.749**
- Role: `07_ethics_and_responsible_research.md`
- Persona: `contrarian.json`
- Interests: `retrieval_augmented_generation.md`
- Methodology: `preregistration_review.md`
- Format: `generic.md`

### Survivor 3 — Balanced
- Citation corr: 0.242 | **Acceptance corr: 0.707**
- Role: `05_clarity_and_presentation.md`
- Persona: `trendy.json`
- Interests: `efficiency_and_compression.md`
- Methodology: `triage_then_deep.md`
- Format: `generic.md`

## Progression

| Gen | Best citation | Best acceptance |
|:---:|:------------:|:---------------:|
| 0   | 0.238        | 0.740           |
| 1   | 0.258        | 0.714           |
| 2   | 0.467        | 0.712           |
| 3   | 0.419        | 0.724           |
| 4   | 0.444        | 0.700           |
| 5   | 0.395        | 0.749           |

## Emerging Patterns

- **Persona convergence**: `trendy.json` dominates (3 of 4 survivors). The "trendy" persona may calibrate better to what gets cited and accepted.
- **Interest convergence**: `efficiency_and_compression.md` appears in 3 of 4. This interest area may provide a lens that generalizes well across papers.
- **Citation vs acceptance split**: The best citation predictors (significance + clarity roles, budgeted methodology) differ from the best acceptance predictor (ethics role, contrarian persona, preregistration methodology).
- **Methodology**: `three_stage_review_budgeted.md` favored for citation prediction; `preregistration_review.md` best for acceptance prediction.
