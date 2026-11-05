from pathlib import Path
import os
import shutil
import sys
import subprocess
import tempfile
import unittest

# Linux: GNU ld --gc-sections. macOS: Apple ld -dead_strip (clang).
CC = shutil.which('clang') if sys.platform == 'darwin' else shutil.which('gcc')
GC_FLAG = '-Wl,-dead_strip' if sys.platform == 'darwin' else '-Wl,--gc-sections'

class CalculatorHostTests(unittest.TestCase):
    @unittest.skipUnless(CC, 'A C compiler (gcc or clang) is required')
    def test_editor_and_escape_with_memory_sanitizers(self):
        here = Path(__file__).parent
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / 'editor_test'
            subprocess.run([CC, '-std=c99', '-g', '-O1', '-fsanitize=address,undefined',
                '-fno-omit-frame-pointer', '-ffunction-sections', '-fdata-sections',
                '-Werror=implicit-function-declaration', '-I'+str(here/'host'),
                str(here/'test_calculator.c'), GC_FLAG, '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True, timeout=10,
                           env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0"})
