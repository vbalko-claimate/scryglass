# Meta-Llama 3.1 LoRA Plan

This plan is for a conservative first fine-tune of `Meta-Llama-3.1-8B-Instruct`.

It is intentionally narrow:
- train only on reliable `play/cast` decisions first
- keep heuristics as the tactical pilot
- use the tuned model as a policy scorer / radar, not as an unvalidated autopilot

## Why Llama 3.1 8B

- The repo already performs best on `llama3.1:8b` among the tested local models.
- `8B` is realistic for LoRA on Apple Silicon.
- The ecosystem around Llama + LoRA is mature.

Official references:
- Meta model card: `Meta-Llama-3.1-8B-Instruct`
- Hugging Face PEFT LoRA docs
- Apple MLX / MLX examples for LoRA on Apple Silicon

## Dataset Strategy

Version 1 should only learn:
- `ActionsAvailableReq`
- `Phase_Main1` / `Phase_Main2`
- your actual first non-land spell play on the turn

It should not try to learn yet:
- attacks
- blocks
- target selection
- stack interaction timing

Those parts are currently too noisy in the logs.

## Exported Files

Use:

```bash
uv run python tools/export_lora_dataset.py
```

Or, if you want official bulk card enrichment cached locally:

```bash
uv run python tools/export_lora_dataset.py --fetch-scryfall-oracle
```

This writes:

- `data/training/llama31_action_sft.raw.jsonl`
- `data/training/llama31_action_sft.chat.jsonl`

When enrichment is enabled, examples also include:

- local card knowledge from the `cards` table
- opponent meta labels from `data/meta/meta_decks.json`
- deck strategy metadata from local strategy JSON files
- optional Scryfall oracle bulk fallback for cards missing from the local DB

The exporter assigns:
- `high`: followed recommendation and won match
- `medium`: followed recommendation or won match
- `low`: everything else

Default export keeps only `medium` and `high`.

## Raw Example Shape

Each raw row contains:

- match metadata
- phase / turn
- reliable board snapshot
- enriched card knowledge where available
- deck / matchup metadata where available
- full legal actions list
- chosen action label
- quality flags

Important label fields:

```json
{
  "label": {
    "played_card": "Get Lost",
    "action_index": 2,
    "action_type": "ActionType_Cast",
    "action_text": "Cast Get Lost"
  }
}
```

## Chat/SFT Example Shape

The chat export uses strict action selection:

- system: choose exactly one legal action
- user: compact board state + numbered legal actions
- assistant: strict JSON with `action_index` and `action_text`

Example assistant output:

```json
{"action_index": 2, "action_text": "Cast Get Lost"}
```

This is important because free-text tactical advice is where the current models fail.

## Recommended Training Phases

### Phase 1: Action Selection Only

Train on `chat.jsonl` only.

Goal:
- pick the correct spell or play from legal actions
- stop inventing mana / cards / phases

### Phase 2: Add Higher-Confidence Tactical Tags

After the structured validator exists, add derived tags such as:
- `remove_engine`
- `develop_board`
- `hold_flash`
- `play_around_wipe`
- `push_lethal`

These should be extra supervision, not the primary label.

### Phase 3: Preference Training

Once enough data exists, add preference data:
- chosen action
- rejected legal alternatives
- short outcome proxy

That is a better fit for DPO or reward modeling later, not for the first LoRA.

## Minimal Hyperparameter Starting Point

These are starting points, not tuned values:

- base model: `Meta-Llama-3.1-8B-Instruct`
- method: `LoRA`
- rank `r`: `16`
- `lora_alpha`: `32`
- `lora_dropout`: `0.05`
- max sequence length: `2048`
- learning rate: `1e-5` to `2e-5`
- micro-batch: `1` or `2`
- gradient accumulation: `8` to `16`
- stop early if validation starts copying bad habits like overcommitting into lethal crackback

## MLX vs Ollama

Use `Ollama` for baseline inference and fast integration.

Use `MLX` when:
- you start training adapters on Apple Silicon
- you want Apple-native inference for the tuned adapter
- you want tighter control over training and adapter loading

In short:
- baseline serving: `Ollama`
- custom Llama LoRA work: `MLX` is a strong next step

## Guardrails Before Fine-Tune

Do these before trusting the model in live play:

1. Force structured output over `LEGAL ACTIONS`
2. Validate mana legality
3. Validate phase legality
4. Reject stale advice
5. Keep heuristics as the final tactical gate

Without this, the fine-tune will mostly learn prettier hallucinations.

## Practical Next Steps

1. Export dataset and inspect size / balance
2. Manually review the first `100-200` rows
3. Add a structured output validator in the app
4. Fine-tune `Meta-Llama-3.1-8B-Instruct` with LoRA
5. Evaluate only on held-out matches from this DB

## Suggested Success Criteria

Do not judge the tuned model on prose quality.

Judge it on:
- exact legal action match rate
- zero invented cards / targets
- zero mana violations
- lower stale advice rate
- better `must-answer` prioritization

## Future Architecture: The Scryglass Forge

The system is evolving from a simple rule-based advisor into a **three-tier, self-improving ecosystem** (Data Flywheel) that leverages Simlab for optimization and Scryglass for live play. 

The core Python engine (`advisor/heuristics.py`) remains the absolute authority (the "laws of physics" - legality, ward, lethal). Above it sit three evolutionary layers:

### Tier 1: GA Parametric Optimization (The Optimizer)
*   **Concept:** Fast, CPU-bound mutation of strategy files.
*   **Mechanism:** When a user creates a new deck, Simlab runs a Genetic Algorithm to mutate the strategy JSON (adjusting weights for aggression, mana holding, interaction priority). It runs hundreds of headless matches against Meta decks using the Forge AI.
*   **Output:** A perfectly tuned, deck-specific `custom_deck.json` with mathematically proven priority weights.

### Tier 2: LLM as Rule Engineer (The Architect)
*   **Concept:** Large, capable LLMs (e.g., GPT-4o, Claude 3.5) act as analysts, not live players.
*   **Mechanism:** Simlab feeds match logs of *losses* to the LLM. The prompt asks the LLM to identify *why* the deck lost and to author a new, valid JSON rule (per `rule_contract.md`) to prevent that specific strategic error.
*   **Output:** New strategic concepts (e.g., "Hold board wipes against Control") are automatically codified into JSON rules, validated by Simlab, and pushed back to Tier 1 for weight optimization.

### Tier 3: Internalization via SFT/DPO (The Brain)
*   **Concept:** Training a local, fast LLM (Meta-Llama 3.1 8B) for live inference.
*   **Mechanism:** Tiers 1 and 2 generate massive amounts of "Golden Data"—trajectories where the system played optimally using complex, multi-layered JSON rules. Instead of complex live RL, we use **Direct Preference Optimization (DPO)** or SFT. We teach the local model to mimic this expert behavior.
*   **Output:** The local model internalizes the complex tactics. The live Scryglass client runs this lightweight model locally, providing cutting-edge advice without relying on cloud APIs or slow real-time rule parsing.
