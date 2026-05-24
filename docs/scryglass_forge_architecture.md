## Future Architecture: The Scryglass Forge

The system is evolving from a simple rule-based advisor into a **three-tier, self-improving ecosystem** (Data Flywheel) that leverages Simlab for optimization and Scryglass for live play. 

### Architecture Diagram

```text
+-------------------------------------------------------------+
|                 Scryglass Core Engine                       |
|      (heuristics.py - "Laws of Physics", Ward, Lethal)      |
+------------------------------+------------------------------+
                               | (provides hard constraints)
                               v
+-------------------------------------------------------------+
|                    The Simlab Factory                       |
|                                                             |
|  +--------------------+             +--------------------+  |
|  | Tier 2: Architect  |             | Tier 1: Optimizer  |  |
|  | (GPT-4o / Claude)  | --writes--> | (Genetic Algorithm)|  |
|  | Analyzes loss logs |   JSONs     | Tunes JSON weights |  |
|  | Invents new rules  |             | Plays 100s of games|  |
|  +--------------------+             +--------------------+  |
|           ^                                 |               |
|           | (loss logs)                     | (Golden Data) |
+-----------|---------------------------------|---------------+
            |                                 |
            +---------------------------------+
                               |
                               v
+-------------------------------------------------------------+
|                    The Training Forge                       |
|                                                             |
|  +-------------------------------------------------------+  |
|  |                 Tier 3: The Brain                     |  |
|  |               (SFT / DPO Fine-tuning)                 |  |
|  |      Teaches Llama 3.1 8B to mimic the tuned JSONs    |  |
|  +-------------------------------------------------------+  |
+------------------------------+------------------------------+
                               |
                               v
+-------------------------------------------------------------+
|                 Live Scryglass Client                       |
|                                                             |
|  +--------------------+             +--------------------+  |
|  |   Tuned JSONs      |      +      | Local Llama 3.1 8B |  |
|  | (from Tier 1 & 2)  |             |    (from Tier 3)   |  |
|  +--------------------+             +--------------------+  |
+-------------------------------------------------------------+
```

### Tier Breakdown

The core Python engine (`advisor/heuristics.py`) remains the absolute authority (the "laws of physics" - legality, ward, lethal). Above it sit three evolutionary layers:

#### Tier 1: GA Parametric Optimization (The Optimizer)
*   **Concept:** Fast, CPU-bound mutation of strategy files.
*   **Mechanism:** When a user creates a new deck, Simlab runs a Genetic Algorithm to mutate the strategy JSON (adjusting weights for aggression, mana holding, interaction priority). It runs hundreds of headless matches against Meta decks using the Forge AI.
*   **Output:** A perfectly tuned, deck-specific `custom_deck.json` with mathematically proven priority weights.

#### Tier 2: LLM as Rule Engineer (The Architect)
*   **Concept:** Large, capable LLMs (e.g., GPT-4o, Claude 3.5) act as analysts, not live players.
*   **Mechanism:** Simlab feeds match logs of *losses* to the LLM. The prompt asks the LLM to identify *why* the deck lost and to author a new, valid JSON rule (per `rule_contract.md`) to prevent that specific strategic error.
*   **Output:** New strategic concepts (e.g., "Hold board wipes against Control") are automatically codified into JSON rules, validated by Simlab, and pushed back to Tier 1 for weight optimization.

#### Tier 3: Internalization via SFT/DPO (The Brain)
*   **Concept:** Training a local, fast LLM (Meta-Llama 3.1 8B) for live inference.
*   **Mechanism:** Tiers 1 and 2 generate massive amounts of "Golden Data"—trajectories where the system played optimally using complex, multi-layered JSON rules. Instead of complex live RL, we use **Direct Preference Optimization (DPO)** or SFT. We teach the local model to mimic this expert behavior.
*   **Output:** The local model internalizes the complex tactics. The live Scryglass client runs this lightweight model locally, providing cutting-edge advice without relying on cloud APIs or slow real-time rule parsing.
