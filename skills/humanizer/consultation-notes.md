# Multi-AI Consultation Notes: Academic Humanizer
> Date: 2026-03-27
> Purpose: Gather insights for converting generic humanizer to academic paper version

---

## 1. Gemini Round 1: Academic AI Detection Features

### Academic-specific AI patterns:
- **Overuse of decorative vocabulary**: delve into, explore the intricacies of, shed light on, meticulously, comprehensively, robust, paradigm-shifting, unraveling, myriad, paramount, cutting-edge, unprecedented, indispensable, harness
- **Formulaic connectors**: Furthermore, Moreover, In addition, Consequently, Hence, Thus, Therefore, However, Nonetheless, Conversely, In conclusion, In light of this, It is imperative that, It is evident that
- **Overly formal/indirect constructions**: "It is hypothesized that..." instead of "We hypothesize that..."; "The present study endeavors to investigate..." instead of "This study investigates..."
- **Shallow summaries**: "This work contributes to a deeper understanding of...", "The implications are far-reaching"
- **Lack of authorial voice**: Even coverage of all points without emphasis or critical evaluation

### Academic whitelist (reasonable in context):
- crucial, highlight, underscore, enhance, significant, pivotal, additionally
- These are legitimate academic discourse markers; the issue is mechanical/indiscriminate use

### Frequency thresholds (per 5000 words):
- Furthermore/Moreover/Additionally: each 2-5 times, total ≤10
- However/Nonetheless: each 3-7 times
- In conclusion/To summarize: 1 each
- AI-flag: any single connector >8-10 times

### Section-specific patterns:
- **Abstract**: Template openings ("This study investigates..."), vague results ("significant improvements"), hollow conclusions ("deeper understanding")
- **Introduction**: Generic background, listing-style literature review, formulaic gap statement ("Despite numerous studies, a gap remains...")
- **Methods**: Generic verbs without specific parameters, missing justifications
- **Results**: Simple table/figure narration without insight, mechanical stats reporting
- **Discussion**: Restating results, shallow explanations, template limitations ("sample size"), vague future directions

---

## 2. Gemini Round 2: Operational Rules

### Transition examples (AI vs Human):
- AI uses explicit connectors at paragraph start; humans use logical flow, topic sentences, pronoun references

### Hedging AI vs Human:
- AI: uniform hedging ("may indicate", "seems to suggest")
- Human: context-specific hedging with reasoning

### Reviewer first-impression signals (by sensitivity):
1. Abnormally fluent but shallow language
2. Over-generalized statements
3. Repetitive connectors/phrases
4. Lack of innovative/challenging viewpoints
5. Unnaturally long/complex sentences
6. Inaccurate/fabricated citations
7. Template-like limitations discussion
8. Overly formal or context-mismatched terminology

### Blacklist (two tiers):
**Must Replace:**
- In the digital age, plays a pivotal role, It goes without saying, It is evident that, There is no doubt that, indispensable, unprecedented, disruptive, profound impact, of paramount importance, It is worth emphasizing that, growing concern/attention, In today's rapidly evolving world

**Control Frequency:**
- Furthermore, Moreover, However, Therefore, Consequently, In conclusion, This paper aims to, This study explores, importance, significance, challenges, opportunities, prospects, complexity, multifaceted, various, diverse

### Frequency thresholds (per 1000 words):
- Mechanical transitions: ≤1-2
- Must-replace words: 0 (avoid completely)
- Control-frequency words: ≤3-5
- AI-style hedging: ≤1-2
- Fixed paragraph-opening patterns: ≤1-2

---

## 3. NotebookLM: Source-Grounded Insights

### Key detection dimensions (from uploaded documents):
1. **Burstiness**: AI text has uniform sentence length/structure; humans vary naturally
2. **Predictability (Perplexity)**: AI chooses most probable word combinations; lacks surprising expressions
3. **Over-polished**: Too smooth, no quirks or non-standard academic expressions
4. **Linear logic**: AI follows strict task execution; humans have natural tangents

### Signature AI markers:
- **Words**: delve, elevate, robust, enhanced, innovative, practical solutions
- **Structures**:
  - Triplets (A, B, and C) - AI loves groups of three
  - Parallel constructions ("It's not just about X, it's about Y")
  - Formulaic openings: "This study...", "In conclusion...", "Therefore...", "Nonetheless..."
  - Em dash overuse

### Section patterns:
- **Introduction**: Grand hollow openings ("XX field has attracted researchers for centuries...")
- **Problem statement**: Literature review → keyword stacking → conclusion (linear)
- **Conclusion**: Simple repetition, lacks deep reflection on limitations
- **Format**: Loves bullet points and short paragraphs

### De-AI strategies (from sources):
1. **Restructure sentences** - don't just swap synonyms, change sentence architecture
2. **Preserve core terminology** - never alter scientific keywords
3. **Add academic hedging** - AI is often too absolute; add "it appears to", "possibly", "may"
4. **Break patterns** - insert human-written sentences among AI paragraphs
5. **Use specialized tools** - academic-trained tools over generic ChatGPT

### Human vs AI essential differences:
1. **Authorial voice**: unique insights, critical thinking, nuanced positions
2. **Knowledge depth**: connecting seemingly unrelated but logically linked knowledge
3. **Experience injection**: specific anecdotes, first-person observations
4. **Natural imperfections**: quirks and unique language habits

### Tool development recommendations:
- Focus on increasing burstiness (sentence length variation)
- Introduce academic hedging
- Auto-restructure triplet patterns
- Guard against over-modifying scientific keywords

---

## Synthesis: Key Themes Across All Sources

### Universal agreement:
1. **Sentence uniformity** is the #1 tell - AI produces even-length, even-structure sentences
2. **Formulaic connectors** at paragraph openings are immediately suspicious
3. **Triplet/rule-of-three** patterns are strongly AI-associated
4. **Lack of critical depth** - AI summarizes but doesn't truly analyze
5. **Section-specific templates** are recognizable (especially Intro and Discussion)

### Key tension to resolve:
- Words like "significant", "highlight", "crucial" ARE legitimate in academic writing
- The issue is FREQUENCY + DISTRIBUTION + CONTEXT, not the words themselves
- Need a three-tier system: ban / limit / allow

### Academic-specific additions needed (not in original humanizer):
1. Burstiness checker (sentence length variation)
2. Section-aware rules (different checks for Abstract vs Methods vs Discussion)
3. Academic hedging calibration
4. Triplet pattern detection
5. Authorial voice injection guidance
6. Transition diversity scoring
7. Scientific terminology whitelist protection
