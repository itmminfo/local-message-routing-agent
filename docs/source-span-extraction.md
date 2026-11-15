# Constrained literal issue extraction

The extractor still reads the original message as natural text, without tools
or a recipient list. Its JSON schema now permits only literal source substrings
at Unicode word/punctuation boundaries, plus the full original message. The
backend constructs these options and copies accepted source slices itself.
The model cannot make an accepted quote by correcting spelling, case or Polish
diacritics. There is no normalization or fuzzy matching.

All contiguous combinations of lexical units are offered, so an actual issue
and an instruction in the same sentence can be separated. Entire sentences are
not indivisible units. The full source is an explicit model option, not a
fallback or automatic expansion after an invalid selection.

The prompt asks the model to keep the whole original when it contains no router
instructions. Otherwise it must remove those instructions while retaining all
factual issue context, including facts on both sides of punctuation. This is a
model instruction, not a proof of semantic completeness or injection resistance.

The original exact-quote validator remains unchanged. It checks strict fields,
types, counts, duplicates, tools and a disjoint source assignment. The selector
also verifies membership in the offered source grammar. ReviewService performs
its existing independent quote validation before saving a suggestion.

The classifier still receives only accepted issue_quotes. The persisted
suggestion, UI, recipient allowlist, approval, delivery, CAS and replay handling
are unchanged. A model proposal does not approve or send a message. Delivery
still requires an explicit operator choice and confirmation, and the local
sender guard still permits only Mailpit.

## Bounded generation

The API retains its 5000-character source limit. Proposal generation additionally
rejects more than 128 lexical units, more than 3072 UTF-8 bytes of serialized
selection messages, or a schema larger than 1 MiB. The grammar budget is counted
incrementally before retaining each distinct option. The unit and input budgets
are checked before constructing the quadratic candidate pool.

These are new availability restrictions, even below the API character limit.
Over-budget input produces an unavailable suggestion before inference. It is
never truncated. The operator can still select a department manually and approve
it explicitly. The byte budget is conservative for the measured local 4096
context profile; it is not a tokenizer measurement for arbitrary models or chat
templates. No model options or runtime dependencies were added.

The old 115 frozen inputs fit the budgets. Tests also exercise a repeated
500-character ordinary input, Unicode/CRLF, the exact schema-size boundary,
oversized input and a large distinct candidate pool. This does not promise a
proposal for every 5000-character input.

## Validation and known limits

Offline tests retain the original exhaustive quote-assignment oracle. New tests
cover source-only grammar choices, exact copying, membership, context across a
semicolon, sub-sentence exclusion capability and bounded construction. Stubs do
not establish real-model semantic quality.

Real-model development controls rejected the numeric token-range prototype:
it accepted inline and instruction-only attacks. The accepted design returns to
natural source text and literal quote output. Development checks on the chosen
grammar retained the portal/HTTP403 context and rejected those two attacks.
They still left two empty selections, including a referral and a question with
an injected routing instruction. No retry, forced selection or full-input rescue
was added. A complete kitchen-appliance quote also exposed an existing
classifier limitation; the routing policy is unchanged.

The full comparison uses the original 115 frozen inputs plus 30 new frozen
inputs, with separate strict, boundary and instruction-only denominators. Each
version is repeated after API/Ollama restart. All quality runs reject their
previews without approval or SMTP. Synthetic mock E2E separately exercises the
approval/delivery plumbing against an isolated local Mailpit; it does not prove
physical human presence or real-model quality.
