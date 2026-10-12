"""Sparse row index over a CSV so files of any size open instantly and scroll freely (Linux and Windows)."""
import csv
import mmap
import os
import threading
from array import array
from bisect import bisect_right
from collections import OrderedDict

import numpy as np

INDEX_CHUNK = 64 << 20  # bytes scanned per indexing step
SEARCH_CHUNK = 4 << 20  # bytes scanned per search step (small so the UI stays responsive)
CACHE_BLOCKS = 512
NL, QUOTE = 10, 34


def sniffDelimiter(sample):
    text = sample.decode('utf-8', 'replace')
    try:
        return csv.Sniffer().sniff(text, delimiters=',;\t|').delimiter
    except csv.Error:
        # Fall back to the most frequent candidate in the first line
        first = text.split('\n', 1)[0]
        return max(',;\t|', key=first.count)


def sniffEncoding(sample):
    try:
        sample.decode('utf-8')
        return 'utf-8'
    except UnicodeDecodeError as e:
        # A multi-byte char cut off at the end of the sample is still utf-8
        return 'utf-8' if e.start >= len(sample) - 4 else 'cp1252'


class CsvIndex:
    """Keeps the byte offset of every `block`-th row; any row is found by parsing at most one block."""

    def __init__(self, path, delimiter=None, encoding=None, hasHeader=True, quoteAware=True, block=128):
        self.path = path
        self.size = os.path.getsize(path)
        self.file = open(path, 'rb')
        self.mm = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ) if self.size else b''
        self.block = block
        self.quoteAware = quoteAware
        self.hasHeader = hasHeader
        sample = self.mm[:1 << 20]
        self.start = 3 if sample.startswith(b'\xef\xbb\xbf') else 0
        self.delimiter = delimiter or sniffDelimiter(sample[self.start:1 << 16])
        self.encoding = encoding or sniffEncoding(sample)
        self.blockStarts = array('q', [self.start])
        self.rowsIndexed = 0
        self.indexedEnd = self.start
        self.progress = 0.0
        self.done = False
        self.cancelled = False
        self.lock = threading.Lock()
        self.rangeCache = OrderedDict()
        self.rowCache = OrderedDict()

    # ---------- indexing ----------

    def build(self):
        # Whole-file scans read through their own handle instead of the map, so memory stays flat on every OS
        with open(self.path, 'rb') as f:
            self._build(f)

    def _build(self, f):
        size, block = self.size, self.block
        pos, carry, rowNo = self.start, 0, 0
        chunk = np.empty(INDEX_CHUNK, dtype=np.uint8)
        f.seek(pos)
        while pos < size and not self.cancelled:
            got = f.readinto(chunk)
            if not got:
                break
            end = pos + got
            buf = chunk[:got]
            nl = np.flatnonzero(buf == NL)
            if self.quoteAware:
                quotes = np.flatnonzero(buf == QUOTE)
                if len(quotes):
                    # A newline ends a row only when an even number of quotes precede it
                    nl = nl[((np.searchsorted(quotes, nl) + carry) & 1) == 0]
                    carry = (carry + len(quotes)) & 1
            del buf
            if len(nl):
                starts = nl + (pos + 1)
                first = (-(rowNo + 1)) % block
                with self.lock:
                    self.blockStarts.extend(starts[first::block].tolist())
                    rowNo += len(starts)
                    self.rowsIndexed = rowNo
                    self.indexedEnd = int(starts[-1])
            pos = end
            self.progress = (pos - self.start) / max(1, size - self.start)
        if not self.cancelled:
            with self.lock:
                # Last row without a trailing newline
                if self.indexedEnd < pos:
                    self.rowsIndexed += 1
                    self.indexedEnd = pos
                self.progress = 1.0
                self.done = True

    @property
    def dataRows(self):
        return max(0, self.rowsIndexed - (1 if self.hasHeader else 0))

    def close(self):
        self.cancelled = True
        self.rangeCache.clear()
        self.rowCache.clear()
        try:
            if self.size:
                self.mm.close()
        except BufferError:
            pass
        self.file.close()

    # ---------- row access ----------

    def _blockBounds(self, b):
        start = self.blockStarts[b]
        if (b + 1) * self.block <= self.rowsIndexed and b + 1 < len(self.blockStarts):
            return start, self.blockStarts[b + 1], True
        return start, self.indexedEnd, self.done

    def blockRanges(self, b):
        """Absolute start offsets of the rows in block b, plus the block end."""
        with self.lock:
            hit = self.rangeCache.get(b)
            if hit is not None:
                self.rangeCache.move_to_end(b)
                return hit
            start, end, complete = self._blockBounds(b)
        buf = np.frombuffer(self.mm, dtype=np.uint8, count=end - start, offset=start) if end > start else np.zeros(0, np.uint8)
        nl = np.flatnonzero(buf == NL)
        if self.quoteAware:
            quotes = np.flatnonzero(buf == QUOTE)
            if len(quotes):
                nl = nl[(np.searchsorted(quotes, nl) & 1) == 0]
        del buf
        starts = np.concatenate(([start], nl + (start + 1)))
        starts = starts[starts < end] if end > start else starts[:0]
        rowsInBlock = min(self.block, self.rowsIndexed - b * self.block)
        result = (starts[:rowsInBlock], end)
        if complete:
            with self.lock:
                self.rangeCache[b] = result
                if len(self.rangeCache) > CACHE_BLOCKS:
                    self.rangeCache.popitem(last=False)
        return result

    def rowBytes(self, fileRow):
        """Raw bytes of a row without its line ending."""
        starts, end = self.blockRanges(fileRow // self.block)
        j = fileRow % self.block
        s = int(starts[j])
        e = int(starts[j + 1]) if j + 1 < len(starts) else end
        raw = self.mm[s:e]
        if raw.endswith(b'\n'):
            raw = raw[:-1]
        if raw.endswith(b'\r'):
            raw = raw[:-1]
        return raw

    def parse(self, raw):
        text = raw.decode(self.encoding, 'replace')
        if not text:
            return []
        try:
            return next(csv.reader([text], delimiter=self.delimiter, strict=False), [])
        except csv.Error:
            return text.split(self.delimiter)

    def blockRows(self, b):
        with self.lock:
            hit = self.rowCache.get(b)
            if hit is not None:
                self.rowCache.move_to_end(b)
                return hit
            complete = self._blockBounds(b)[2]
        base = b * self.block
        starts, _ = self.blockRanges(b)
        rows = [self.parse(self.rowBytes(base + j)) for j in range(len(starts))]
        if complete:
            with self.lock:
                self.rowCache[b] = rows
                if len(self.rowCache) > CACHE_BLOCKS:
                    self.rowCache.popitem(last=False)
        return rows

    def row(self, fileRow):
        if fileRow >= self.rowsIndexed:
            return []
        rows = self.blockRows(fileRow // self.block)
        j = fileRow % self.block
        return rows[j] if j < len(rows) else []

    def header(self):
        return self.row(0) if self.hasHeader and self.rowsIndexed else []

    def rowAt(self, offset):
        """(fileRow, rowEnd) of the row containing a byte offset."""
        b = bisect_right(self.blockStarts, offset) - 1
        b = min(b, (self.rowsIndexed - 1) // self.block)
        starts, end = self.blockRanges(b)
        j = int(np.searchsorted(starts, offset, side='right')) - 1
        rowEnd = int(starts[j + 1]) if j + 1 < len(starts) else end
        return b * self.block + j, rowEnd

    def dataStart(self):
        if not self.hasHeader or self.rowsIndexed < 2:
            return self.start if not self.hasHeader else self.indexedEnd
        return int(self.blockRanges(0)[0][1]) if self.block > 1 else self.blockStarts[1]


class Search:
    """Scans the whole file for a term in a background thread; hits are data-row numbers in file order."""

    def __init__(self, index, term, caseSensitive=False, column=None):
        self.index = index
        self.term = term
        self.caseSensitive = caseSensitive
        self.column = column
        self.hits = array('q')
        self.progress = 0.0
        self.done = False
        self.cancelled = False
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def cancel(self):
        self.cancelled = True

    def run(self):
        idx = self.index
        pattern = self.term.encode(idx.encoding, 'replace')
        # bytes.lower folds ASCII only, applied to both sides so they agree
        if not self.caseSensitive:
            pattern = pattern.lower()
        with open(idx.path, 'rb') as f:
            self.scan(f, pattern)
        self.progress = 1.0
        self.done = True

    def scan(self, f, pattern):
        idx = self.index
        end = idx.indexedEnd
        first = idx.dataStart()
        pos = first
        offsetRow = 1 if idx.hasHeader else 0
        span = max(1, end - first)
        while pos < end and not self.cancelled:
            chunkEnd = min(pos + SEARCH_CHUNK, end)
            f.seek(pos)
            buf = f.read(min(chunkEnd + len(pattern) - 1, end) - pos)
            if not self.caseSensitive:
                buf = buf.lower()
            nextPos = chunkEnd
            i = buf.find(pattern)
            while i != -1:
                fileRow, rowEnd = idx.rowAt(pos + i)
                if self.matches(fileRow):
                    self.hits.append(fileRow - offsetRow)
                if rowEnd >= chunkEnd:
                    nextPos = rowEnd
                    break
                i = buf.find(pattern, rowEnd - pos)
            pos = nextPos
            self.progress = min(1.0, (pos - first) / span)

    def matches(self, fileRow):
        if self.column is None:
            return True
        row = self.index.parse(self.index.rowBytes(fileRow))
        if self.column >= len(row):
            return False
        cell = row[self.column]
        return self.term in cell if self.caseSensitive else self.term.lower() in cell.lower()


class Export:
    """Writes the header plus the given data rows, byte for byte, to a new file."""

    def __init__(self, index, rows, path):
        self.index, self.rows, self.path = index, rows, path
        self.progress = 0.0
        self.done = False
        self.cancelled = False
        self.error = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def run(self):
        idx = self.index
        offsetRow = 1 if idx.hasHeader else 0
        try:
            with open(self.path, 'wb') as out:
                if idx.hasHeader:
                    out.write(idx.rowBytes(0) + b'\n')
                total = max(1, len(self.rows))
                for n, r in enumerate(self.rows):
                    if self.cancelled:
                        break
                    out.write(idx.rowBytes(r + offsetRow) + b'\n')
                    if n % 4096 == 0:
                        self.progress = n / total
        except OSError as e:
            self.error = str(e)
        self.progress = 1.0
        self.done = True
