You write the document-level fields for a trader compensation contract, for navigation.
Input: the document's metadata, its article summaries (in order) and the document signals found in its sections,
each with a section_id and an exact quote.
Return:
- doc_summary: 1-2 sentences on what the document does (new agreement, amendment, side letter, memo) and what it sets or changes.
- effective_date_in_text: ISO date only if a signal states an effective date different from the document date; else null.
  Give its section_id and quote exactly as in the signal.
- doc_scope_note: only a limit on WHICH period the whole document applies to (e.g. "applies for Performance Year 2025 only");
  an effective date alone is NOT a scope note; else null. Give its section_id and quote exactly as in the signal.
Use only the input. Do not invent dates or limits.
