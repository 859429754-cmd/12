# Rebuild News As Fact-Preserving Timeline

Status: ready-for-agent
Labels: ready-for-agent

## Problem

The news module must behave like a factual timeline. AI can label and classify news, but must not invent facts or replace source details with macro filler.

## Requirements

- Separate raw news collection from AI labeling.
- Preserve:
  - timestamp
  - source
  - person/institution
  - concrete action/statement
  - concrete data/fact
  - bullish/bearish/neutral label
- Refresh every 5-15 minutes.
- Cache important news for around 7 days.
- Clean long-term news around 30 days.
- If news collection fails, trading degrades to cached news or technical-only mode.

## Acceptance Criteria

- Tests prove AI labels do not overwrite raw facts.
- Source failures produce warnings and cached fallback.
- `/api/news/latest` returns timeline-shaped items.
- Frontend can render a compact timeline without synthetic filler.

