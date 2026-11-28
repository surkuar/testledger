"""Optional LLM-written narrative for the Quality Investment Memo.

Off by default. When ``analyze --narrative`` is given, a short plain-language
paragraph is generated from the memo's own figures and inserted into the memo,
clearly labelled as AI-generated and naming the model.

Privacy rules (also documented in docs/methodology.md):

* Only ``summary.json`` figures, the top write-off rows and the assumptions
  are sent to the model. Test source code, failure messages, stack traces
  and repository content are never sent.
* The reply is rejected when it contains a dollar figure or percentage that
  does not trace back to the input payload; the memo then falls back to the
  plain template.

Providers: any OpenAI-compatible local endpoint (e.g. Ollama) through plain
HTTP, and the hosted OpenAI API through the optional ``openai`` package
(``pip install testledger[narrative]``). Model and endpoint come from the
``narrative:`` config block; the API key comes only from the environment
variable named there.
"""

from testledger.narrative.generate import (
    NarrativeError,
    generate_narrative,
    untraced_figures,
)

__all__ = ["NarrativeError", "generate_narrative", "untraced_figures"]
