# ovirt-imageio
# Copyright (C) 2018 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

from __future__ import absolute_import

import logging
import os

from .. import errors
from .. import nbd
from .. import nbdutil

from . import image

log = logging.getLogger("backends.nbd")

Error = nbd.Error


def open(url, mode, sparse=True, dirty=False, **options):
    """
    Open a NBD backend.

    Arguments:
        url (url): parsed NBD url (e.g. "nbd:unix:/socket:exportname=name")
        mode (str): "r" for readonly, "w" for write only, "r+" for read write.
            Note that writeable backend does not guarantee that the underling
            nbd server is writable. The server must be configured to allow
            writing.
        sparse (bool): ignored, NBD backend does not support sparseness. This
            must be controlled by the nbd server. It seems that qemu-nbd and
            qemu always deallocate space when zeroing.
        dirty (bool): if True, configure the client to report dirty extents.
            Can work only when connecting to qemu during incremental backup.
        **options: ignored, nbd backend does not have any options.
    """
    client = nbd.open(url, dirty=dirty)
    try:
        return Backend(client, mode)
    except:  # noqa: E722
        client.close()
        raise


class Backend(object):
    """
    NBD backend.
    """

    def __init__(self, client, mode):
        if mode not in ("r", "w", "r+"):
            raise ValueError("Unsupported mode %r" % mode)
        log.info("Open backend address=%r export_name=%r",
                 client.address, client.export_name)
        self._client = client
        self._mode = mode
        self._position = 0
        self._dirty = False

    # Backend interface

    def readinto(self, buf):
        if not self.readable():
            raise IOError("Unsupported operation: readinto")

        length = min(len(buf), self._client.export_size - self._position)
        if length <= 0:
            # Client should not send NBD_CMD_READ with zero length, and
            # request after the end of file is invalid.
            return 0

        with memoryview(buf)[:length] as view:
            self._client.readinto(self._position, view)

        self._position += length
        return length

    def write(self, buf):
        if not self.writable():
            raise IOError("Unsupported operation: write")
        self._client.write(self._position, buf)
        length = len(buf)
        self._position += length
        self._dirty = True
        return length

    def zero(self, length):
        if not self.writable():
            raise IOError("Unsupported operation: zero")
        self._client.zero(self._position, length)
        self._position += length
        self._dirty = True
        return length

    def flush(self):
        self._client.flush()
        self._dirty = False

    def tell(self):
        return self._position

    def seek(self, pos, how=os.SEEK_SET):
        if how == os.SEEK_SET:
            self._position = pos
        elif how == os.SEEK_CUR:
            self._position += pos
        elif how == os.SEEK_END:
            self._position = self._client.export_size + pos
        return self._position

    def close(self):
        log.info("Close backend address=%r", self._client.address)
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, t, v, tb):
        try:
            self.close()
        except Exception:
            # Do not hide the original error.
            if t is None:
                raise
            log.exception("Error closing")

    def extents(self, context="zero"):
        if context not in ("zero", "dirty"):
            raise errors.UnsupportedOperation(
                "Backend nbd does not support {} extents".format(context))

        # If server does not support base:allocation, we can safely report one
        # data extent like other backends.
        if context == "zero" and not self._client.base_allocation:
            yield image.ZeroExtent(0, self._client.export_size, False)
            return

        # If dirty extents are not available, client may be able to use zero
        # extents for eficient download, so we should not fake the response.
        if context == "dirty" and self._client.dirty_bitmap is None:
            raise errors.UnsupportedOperation(
                "NBD export {!r} does not support dirty extents"
                .format(self._client.export_name))

        dirty = context == "dirty"
        start = 0
        for ext in nbdutil.extents(self._client, dirty=dirty):
            if dirty:
                yield image.DirtyExtent(start, ext.length, ext.dirty)
            else:
                yield image.ZeroExtent(start, ext.length, ext.zero)
            start += ext.length

    @property
    def block_size(self):
        # qemu always reports minium_block_size=1, so caller never needs to
        # align requests and read more data than needed.
        return self._client.minimum_block_size

    # Debugging interface

    def readable(self):
        return self._mode in ("r", "r+")

    def writable(self):
        return self._mode in ("w", "r+")

    @property
    def dirty(self):
        """
        Returns True if backend was modified and needs flushing.
        """
        return self._dirty

    @property
    def sparse(self):
        # TODO:
        # - can we get this info from the backend?
        return True

    @property
    def name(self):
        return "nbd"

    def size(self):
        return self._client.export_size