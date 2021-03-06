# Copyright (c) 2011 The Native Client Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import re
import subprocess


def DecodeObjdump(lines):
  prev_disasm = ''
  prev_bytes = ''
  for line in lines:
    match = re.match('\s*[0-9a-f]+:\s*((\S\S )+)\s*(.*)', line)
    if match is not None:
      bytes = match.group(1)
      disasm = match.group(3)
      if disasm != '' and prev_disasm != '':
        yield prev_bytes, prev_disasm
        prev_bytes = ''
        prev_disasm = ''
      bytes = ''.join([chr(int(part, 16)) for part in bytes.strip().split(' ')])
      prev_bytes += bytes
      prev_disasm += disasm
  if prev_disasm != '':
    yield prev_bytes, prev_disasm


def assert_eq(x, y):
  if x != y:
    raise AssertionError('%r != %r' % (x, y))


assert_eq(list(DecodeObjdump(
      '''
     90e:       8d 82 d0 01 00 00       lea    0x1d0(%edx),%eax
     914:       c7 44 24 08 00 00 00    movl   $0x0,0x8(%esp)
     91b:       00 
     914:       c7 44 24 08 00 00 00    movl   $0x0,0x8(%esp)
     91b:       00 
'''.split('\n'))),
      [('\x8d\x82\xd0\x01\x00\x00', 'lea    0x1d0(%edx),%eax'),
       ('\xc7D$\x08\x00\x00\x00\x00', 'movl   $0x0,0x8(%esp)'),
       ('\xc7D$\x08\x00\x00\x00\x00', 'movl   $0x0,0x8(%esp)'),
       ])


def Decode(filename):
  proc = subprocess.Popen(['objdump', '-M', 'suffix', '-d', filename],
                          stdout=subprocess.PIPE)
  return DecodeObjdump(proc.stdout)
