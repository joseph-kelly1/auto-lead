"""Read selected members out of a huge remote zip without downloading it.

The MassGIS statewide L3 aggregate is ~7.8 GB per release and we need four
Assess tables out of it. S3 serves ranges, and stdlib `zipfile` only needs a
seekable file object, so a ~40-line adapter gets zip64 parsing for free.
"""

import io
import urllib.request


class HttpFile(io.RawIOBase):
    def __init__(self, url: str):
        self.url = url
        self._pos = 0
        req = urllib.request.Request(url, method="HEAD")
        self.size = int(urllib.request.urlopen(req).headers["Content-Length"])

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=io.SEEK_SET):
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self.size}[whence]
        self._pos = max(0, min(self.size, base + offset))
        return self._pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self._pos
        n = min(n, self.size - self._pos)
        if n <= 0:
            return b""
        end = self._pos + n - 1
        req = urllib.request.Request(
            self.url, headers={"Range": f"bytes={self._pos}-{end}"})
        data = urllib.request.urlopen(req).read()
        self._pos += len(data)
        return data

    def readinto(self, b):
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)
