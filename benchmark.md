# LLM Benchmark Report: Performance Analysis on MCP / SWE-bench

This report presents a comparative analysis of **Llama-3.3-70b-versatile** (via Groq) and **Gemini-2.5-flash** (via Google API) integrated within our sandboxed Docker agent infrastructure.

---

## 📊 1. Quantitative Performance Metrics

| Framework / Repo | Evaluated Model | Success | Steps | Total Time | Tokens (Input/Output) | Core Behavioral Pattern |
| :--- | :--- | :---: | :---: | :---: | :---: | :--- |
| **Django** (Forms) | Llama-3.3-70b | **Pass** | 8 | 66.1s | 11,876 / 641 | Verbose reasoning; analyzed deep class inheritance. |
| **SymPy** (Math) | Llama-3.3-70b | **Pass** | 7 | 75.3s | 10,010 / 168 | Methodical; successfully recovered from failed tool regex. |
| **Xarray** (Data) | Gemini-2.5-flash | **Pass** | 4 | 64.9s | 8,725 / 97 | Laser-focused efficiency; zero conversational bloat. |
| **Django** (Multi-DB) | Gemini-2.5-flash | **Pass** | 4 | 54.6s | 5,485 / 125 | Direct execution; instantly targets explicit hints in issue description. |

---

## 🧠 2. Qualitative Model Comparison

### 🦙 Llama-3.3-70b-versatile: The "Academic Engineer"
* **Pros:** Highly resilient diagnostic logic. When its initial regex search failed on SymPy, it methodically read the surrounding file scope (`read_file`) to isolate the typo (`cotm` vs `cothm`).
* **Cons:** Verbose. It constantly comments on its own intent despite strict instructions, leading to higher output token costs (641 tokens on Django).

### ♊ Gemini-2.5-flash: The "Production Robot"
* **Pros:** Absolute code efficiency. On both Xarray and Django (Multi-DB), it outputted **only** clean executable Python blocks with zero conversational fluff, keeping output token consumption at an absolute minimum (97 and 125 tokens).
* **Cons:** Opportunistic explorer. It relies heavily on hints provided directly inside the problem statement (like explicit line numbers or user-suggested patches) to dash straight to the target line without prior independent exploration.

---

## 🛠️ 3. Infrastructure Key Takeaways

> **Token Saver Optimization:** Our custom `run_tests()` log filter cut down over 80% of redundant Conda shell activation outputs and terminal clutter. Without this token-saving layer, context payloads hit 14,000+ tokens, instantly triggering 413/429 API rate limits.

> **Deterministic Control:** Forcing `temperature: 0.0` inside `LLMClient` eliminated any model's tendency to cheat or hallucinate fake memory patches on healthy files. It successfully anchored the execution into strict, observation-driven debugging.

---

## 🎯 4. Final Verdict for Defense

1. **Primary Driver:** Deploy **Gemini-2.5-flash** by default to maximize execution speed, eliminate useless chatter, and drastically minimize API costs on well-documented tasks containing clear pointers.
2. **Escalation Fallback:** Automatically route to **Llama-3.3-70b-versatile** when a test suite throws multi-file tracebacks or when no explicit code pointers or hints are present in the issue description.