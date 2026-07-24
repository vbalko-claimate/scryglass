# Scryglass UI declutter — questionnaire

The app UI grew organically. Goal: decide what stays, what's cut, what's core.
For each item mark **K** (keep) / **C** (cut) / **?** (unsure), and **★** if it
matters DURING a live game (overlay-worthy). Flags: `[dup]` = shown elsewhere too,
`[dev]` = dev/debug/power-user, `[friction]` = needs setup.

## A. Main window (index.html)
1. Profile switcher Focus/Full/Tactical (density presets) - C
2. Nav links (Stats / Review / Manage / Decks) - ?
3. Connection status ("Waiting for match…") - K
4. Turn info + deck-strategy banner - K
5. Vital bar (your/opp life + mana) - K
6. Board view (opp zone / my zone / hand — live) - K
7. Decision HUD (advice + heuristics/LLM subtitle) - K
8. Ask AI button (manual LLM) - C
9. Auto-LLM toggle (run LLM automatically) - ?
10. Backend select (Claude CLI / Ollama / API) - c
11. Match Summary button (LLM)  `[dup: also in Stats]` - ?
12. Export Last Game button - ?
13. "Do Now" section (immediate plays)  `[dup: also in overlay]` - K
14. "Context" section (threat model / matchup / AI) - K
15. Opponent Radar panel (threats)  `[dup: overlap w/ overlay opp line]` - K
16. Footer (library / graveyard / stack / state id / version) - K
17. About overlay - K
18. Debug panel (strategy internals, rules-by-layer)  `[dev]` - K

## B. In-game overlay (overlay.html) — what you see over Arena
19. Peek pill (collapse so it stops covering the board) - K
20. Opponent deck + confidence % - K
21. Lethal banner (you're about to die) - K
22. Combo banner (big swing available) - K
23. Synergy hint (quiet teaching hint) - K
24. Key-play spotlight (the main advice: CAST/ATTACK/BLOCK/…) - K
25. Advice confidence line (off-catalog warning) - K
26. Threat line ("⚠ …") - K
27. "Do Now" list (next 2–3 secondary plays) - K
28. Phase line (T# · phase · your/opp turn) - K
29. Control-hint line (feedback/drag hint) - K
30. Feedback buttons ✓ good / ✗ bad / ⚑ flag - K
31. Match-end debrief (W/L + "Open review") - K
32. Between-match session record (W-L, last 20) - C
33. Reposition (Option+arrow nudge / drag) - K

## C. Manage (manage.html) — 7 tabs + collection bar
34. Refresh collection from MTGA memory  `[friction: sudo setup]` - ?
35. Collection stats (counts, wildcards) - K
36. Strategies tab (per-deck rules; view/edit JSON; import/delete "stubs") - C
37. General Rules tab (universal MTG rules editor) - C
38. Meta Decks tab + "Sync from MTGGoldfish" - ?
39. Decks tab (read-only, "Manage →" to /decks)  `[dup: decks.html]` - C
40. Guides tab (strategy guides, markdown) - C
41. GA Runs tab (optimization runs, Studio status)  `[dev]` - C
42. Cloud Sync tab (account, Sign in, config, Sync now) - ?
43. "Link your email" (6-digit claim)  `[mostly superseded by Sign in]` - ?

## D. Stats (stats.html)
44. Overview cards (matches / wins / WR / avg turns / streak) - K
45. Recent trend chart - K
46. Deck performance table  `[dup: Manage Strategies WR]` - K
47. Matchups (my deck vs opp) - K
48. Color matchups - K
49. Mulligan stats - K
50. My card performance - K
51. Mana curve efficiency - K
52. Advice compliance (follow rate) - K
53. Weakness alerts - K
54. Opponent cards (most-seen) - K
55. Match history + LLM summary + turn-by-turn timeline  `[timeline dup: Review]` - K
56. Life graph - K

## E. Review (review.html) - K
57. Match list (last 10)
58. Match header + summary bar (turns / key moments / advice)
59. Filter (key moments / all turns)
60. Turn timeline w/ advice items  `[dup: Stats turn-by-turn]`

## F. Decks (decks.html) - C all
61. Deck list + Create (paste MTGA list)
62. Editable description
63. Generate Rules: Auto / LLM / Expert  `[Expert = heavy/experimental]`
64. Deploy version
65. Optimize (gauntlet sims, ~30–60s)  `[needs engine sidecar]`
66. Build variants (MAP-Elites portfolio)  `[needs engine sidecar]`
67. Missing cards vs collection
68. Versions list + GA results/matchup bars
69. Deck-list editor (set / new version)
70. Applied suggestions (accepted optimizer swaps + record)

## G. Setup / Loading - ?
71. Setup: 4 readiness checks (engine / cards / MTGA log / rules) + Start
72. Loading: spinner + staged status + error/retry

---

## Cross-cutting notes for the redesign
- **Naming**: the app calls itself 3 things — title "MTGA Play Advisor", header
  "MTGA Advisor", About "Scryglass". Pick one (Scryglass).
- **Nav is inconsistent** per page (index has all 4; Stats has only "Back";
  Review omits Stats; Decks omits Review). Unify into one nav.
- **Duplication to collapse**: deck mgmt (Manage tab vs Decks page); GA results
  (Manage GA Runs vs Decks GA vs Manage Strategies WR); post-game timeline
  (Stats vs Review); LLM match summary (index vs Stats); opp deck+confidence
  (overlay vs index radar).
