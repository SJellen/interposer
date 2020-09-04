# -*- coding: utf-8 -*-
#
# Copyright (c) 2020 Tuono, Inc.
# All Rights Reserved
#
import gzip
import os

from pathlib import Path
from unittest import TestCase

from interposer import Interposer
from interposer import Mode


class InterposedTestCase(TestCase):
    """
    Wraps a test that leverages interposer to record and then play back tests.

    When the environment variable RECORDING is set, the tests in this test
    class will record what they do, depending on what is patched in as a
    wrapper.
    """

    def setUp(self, *args, **kwargs) -> None:
        """
        Prepare for recording or playback based on the test name.

        Arguments:
          recordings (Path): the location of the recordings
        """
        tapedir = kwargs.pop("recordings", None)
        super().setUp(*args, **kwargs)

        assert tapedir, "recordings location must be specified"
        assert isinstance(tapedir, Path), "recordings location must be a pathlib.Path"

        self.mode = Mode.Recording if os.environ.get("RECORDING") else Mode.Playback
        self.tape = tapedir / f"{self.id()}.db"
        if self.mode == Mode.Playback:
            # decompress the recording
            with gzip.open(str(self.tape) + ".gz", "rb") as fin:
                with self.tape.open("wb") as fout:
                    fout.write(fin.read())
        else:
            tapedir.mkdir(parents=True, exist_ok=True)

        self.interposer = Interposer(self.tape, self.mode)
        self.interposer.open()

    def tearDown(self, *args, **kwargs) -> None:
        """
        Finalize recording or playback based on the test name.
        """
        self.interposer.close()
        if self.mode == Mode.Recording:
            # compress the recording
            with self.tape.open("rb") as fin:
                with gzip.open(str(self.tape) + ".gz", "wb") as fout:
                    fout.write(fin.read())

        # self.tape is the uncompressed file - do not leave it around
        self.tape.unlink()

        super().tearDown(*args, **kwargs)
