# Position representation for chess agents: what beats FEN (arXiv survey, Oct 2026)

Question: is there a better way to show a position to an agent (or human) than FEN?
Short answer: yes. FEN is a compact serialization, not a representation designed for
reasoning. The literature converges on four upgrades, ordered by relevance to our
frozen-model harness (no training, one JSON reply per move).

## 1. What the evidence says (newest first)

### 1.1 ASCII diagrams transfer better than FEN (Kim et al., Oct 2026)
- Paper: `Representation-Aligned Auxiliary Supervision for Language Model Adaptation`
  (arXiv:2610.04098, NeurIPS 2026 LP4FM workshop).
- Finding: models process semantically identical FEN and ASCII-board inputs
  "substantially differently". ASCII-trained models transfer to FEN evaluation
  *better* than FEN-trained models transfer to ASCII - even beating the FEN-only
  baseline on FEN. Auxiliary supervision expressed in the model-compatible
  representation helps; tasks that expose environment *dynamics* help most.
- Read for us: keep the ASCII diagram (we already send one). FEN's run-length
  digits and slash separators are tokenizer-hostile; one-character-per-square
  grids match the board's spatial structure. Never send FEN alone.

### 1.2 Spatial board input beats move-sequence input, even with causal masking
  (Junkin & Nathanson, NeurIPS 2025)
- Paper: `Causal Masking on Spatial Data` (arXiv:2510.14825).
- Finding: transformers trained on spatial board states play stronger than ones
  trained on sequential move data - even when the spatial model still uses causal
  masking. Sequentialization (move lists only) throws away structure the model
  can actually use.
- Read for us: the SAN history alone is the weak format; the FEN/diagram block
  carries the strength. We send both, which is correct - but the move *choice*
  must be grounded in the spatial block, not the history.

### 1.3 Verbalizing engine state into text has a measurable cost ("verbalization debt")
  (LLAMIA, EMNLP 2026)
- Paper: `Exploring Collaboration between a language and a non-language agent`
  (arXiv:2609.00474).
- Finding: compressing a chess engine's continuous state into text summaries for
  the LLM underperforms projecting it as learned state tokens straight into the
  token stream. The gap *widens* through training and persists from 4B to 14B.
- Read for us: for frozen prompting we cannot inject learned tokens - but the
  lesson is to pass *facts*, not *judgements*: raw attacker/defender counts, not
  "White is better". (Matches our owner rule: no `??` marks during play.)

### 1.4 Don't serialize at all: fuse a chess encoder (Queen, Oct 2026)
- Paper: `Language Models that Play Chess and Explain Their Moves`
  (arXiv:2610.03695, code: github.com/queen-project/queen).
- Result: 4B model, chess encoder + cross-attention into an instruction-tuned LM,
  QA curriculum plus a Bellman-like self-iteration: 1782 -> 2697 Elo in 7 rounds,
  beating all frontier models on puzzles, with fluent explanations.
- Read for us: the ceiling option if we ever fine-tune. Text serialization is
  the bottleneck they designed around.

### 1.5 Two modalities + consistency check beats one (BBA, Feb 2024)
- Paper: `BBA: Bi-Modal Behavioral Alignment` (arXiv:2402.13577).
- Finding: separate reasoning chains for the board image and the DSL (FEN-like)
  text, then aligned against each other: GPT-4V chess *positional advantage
  prediction* 42.08% -> 46.99%.
- Read for us: our test showed image-only is *worse* than FEN+diagram for all
  three models - consistent with BBA's point that vision alone is not the fix.
  The fix is two views plus a consistency demand, not swapping one view out.

### 1.6 Generated visual intermediates beat pure chain-of-thought (CoI, 2023)
- Paper: `Chain of Images for Intuitively Reasoning` (arXiv:2311.09241).
- Finding: generating images as intermediate reasoning steps beats pure CoT on
  chess tasks.
- Read for us: speculative for now (rendering attack-map overlays as images).
  Parked - text facts first, they are cheaper and cacheable.

### 1.7 The classical answer: plane stacks, not strings (AlphaZero 2017, Maia 2020)
- AlphaZero (arXiv:1712.01815, Methods/Representation): input is an
  8x8x119 stack - 8-ply history of 12 binary piece planes, plus colour,
  move-count, castling-rights, repetition and fifty-move planes, oriented to
  the side to move. Quote: "the neural network architecture is matched to the
  grid-structure of the board." Training was "robust for many reasonable
  choices" - the grid structure is the point, not the exact planes.
- Prior art in the same paper: Giraffe reached IM level with *mobility maps and
  attack/defend maps (lowest valued attacker and defender of each square)*;
  KnightCap reached master level with an *attack-table of which squares are
  attacked or defended by which pieces*.
- Maia (arXiv:2006.01855): same AlphaZero-style input, trained on human games;
  predicts human moves far better than engines. Skill-conditioning redistributes
  attention depth per piece/move type (Maia-3 study, arXiv:2609.23917).
- Read for us: this is the template for a verbalized fact block (see proposal).

### 1.8 Representation is the field's agreed crux; FEN-only fine-tuning caps out
- Mapping study (arXiv:2609.18286, Sep 2026): "LLM research places particular
  emphasis on state representation and generalization."
- ChessLLM (arXiv:2501.17186): FEN-string fine-tuning reaches ~1788 Elo vs
  Stockfish - proof FEN works for learning, and proof of its ceiling.
- ChessBench (arXiv:2402.04494): supervised transformers on Stockfish-annotated
  positions reach 2895 blitz / GM with *no search*. Supervision + annotation
  density beats raw-string training.
- MATE (arXiv:2411.06655): 1M positions with expert strategy/tactic annotations;
  an 8B fine-tune beats GPT/Claude/Gemini at move selection.
- Eval method worth stealing (arXiv:2508.19851): judge state-tracking by
  downstream *legal-move distributions* (affordances), not string match.

## 2. Proposal for our harness (frozen models, no training)

Append a `Tactics facts` block AFTER the legal-moves list (still suffix = cache
prefix intact), computed by python-chess in ~20 lines:

- For each legal move: destination square, captured piece value if any,
  #attackers/#defenders on the destination *after* the move, lowest attacker /
    defender values (Giraffe-style), gives-check flag.
- Own pieces currently en prise (attacked and insufficiently defended), with the
  cheapest winning capture for the opponent - the 1-ply exchange check that
  prior work shows catches ~92/100 permanent material drops.
- No judgements, no marks, no engine scores: pure facts, so the no-cheating rule
  holds exactly as with FEN.

Why this should fix our observed failure mode: DeepSeek's 2026-10-26 loss was
25.Nxf7?? (+11 -> -511) - a hung knight. Every field needed to reject it
(destination attacked twice, defended zero times, lowest attacker = pawn) is
in the fact block. Its thinking shows the count is usually attempted on a
*wrong* board; computed facts remove the board-reading step entirely.

Cost: roughly 2-3k extra tokens per move, all in the uncached suffix. Open
measurement: hung-material moves per 100 moves before/after (the metric from
the research-board proposal), plus invalid-reply rate (must not rise).

## 3. Deliberately not proposed

- Image-only or image-instead-of-FEN: measured worse on our own models (DeepSeek
  F1 0.36 on image-only captures).
- Latent state tokens / encoder fusion (Queen, LLAMIA): needs training or a
  custom model; revisit if we ever fine-tune.
- Swapping FEN out: Stockfish, PGN tooling and caching all key on it; FEN stays
  as the machine-readable anchor, ASCII stays as the spatial view.
