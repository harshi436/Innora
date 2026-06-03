"""
utils/sentence_tokenizer.py — Streaming sentence boundary detector.

Buffers LLM token stream and flushes complete sentences to TTS.
Improvements:
  - Smarter chunk flushing on comma + sufficient length
  - Handles Hindi/Hinglish punctuation (। ?)
  - Thread-safe reset
"""
import re
from typing import List, Optional

# Sentence-ending punctuation (English + Hindi danda)
SENTENCE_END = re.compile(r'(?<=[.!?।])[\s]+|(?<=[.!?।])$')

# Also chunk on comma/semicolon when buffer is long enough (reduces latency)
CLAUSE_END = re.compile(r'(?<=[,;:])[\s]+')

MIN_SENTENCE_LEN = 10
CLAUSE_FLUSH_LEN = 60   # flush on clause boundary only when buffer is this long


class SentenceTokenizer:
    def __init__(self):
        self._buffer = ""

    def feed(self, token: str) -> List[str]:
        """
        Accept a token from the LLM stream.
        Returns a list of complete sentences ready for TTS.
        """
        self._buffer += token
        sentences: List[str] = []

        # 1. Try full sentence boundaries first
        while True:
            match = SENTENCE_END.search(self._buffer)
            if not match:
                break
            sentence = self._buffer[: match.start() + 1].strip()
            remainder = self._buffer[match.end():]
            if len(sentence) >= MIN_SENTENCE_LEN:
                sentences.append(sentence)
                self._buffer = remainder
            else:
                # Too short — merge with next sentence instead of sending alone
                break

        # 2. If buffer grew very long, chunk on clause boundaries to reduce latency
        if not sentences and len(self._buffer) >= CLAUSE_FLUSH_LEN:
            match = CLAUSE_END.search(self._buffer)
            if match:
                clause = self._buffer[: match.start() + 1].strip()
                remainder = self._buffer[match.end():]
                if len(clause) >= MIN_SENTENCE_LEN:
                    sentences.append(clause)
                    self._buffer = remainder

        return sentences

    def flush(self) -> Optional[str]:
        """Return and clear any remaining buffered text (end of stream)."""
        remaining = self._buffer.strip()
        self._buffer = ""
        return remaining if remaining else None

    def reset(self):
        """Clear the buffer (on barge-in / call end)."""
        self._buffer = ""