
import re
import subprocess

import objdump


def MapWildcard(byte):
  if byte == 'XX':
    return '11'
  else:
    return byte


def DisassembleTest(get_instructions, bits):
  fh = open('tmp.S', 'w')
  count = 0
  for bytes, desc in get_instructions():
    asm = '.ascii "%s" /* %s */\n' % (
      ''.join('\\x' + MapWildcard(byte) for byte in bytes), desc)
    fh.write(asm)
    count += 1
  fh.close()
  print 'Checking %i instructions...' % count
  subprocess.check_call(['gcc', '-c', '-m%i' % bits, 'tmp.S', '-o', 'tmp.o'])
  seq = objdump.Decode('tmp.o')
  for index, (bytes, desc) in enumerate(get_instructions()):
    bytes2, disasm_orig = seq.next()
    if len(bytes) != len(bytes2):
      print 'Length mismatch (%i): %r %r versus %r %r' % (
        index, bytes2, disasm_orig, bytes, desc)
    disasm = (disasm_orig
              .replace('0x1111111111111111', 'VALUE64')
              .replace('0x11111111', 'VALUE32')
              .replace('0x1111', 'VALUE16')
              .replace('0x11', 'VALUE8')
              .replace(',', ', '))
    # Canonicalise whitespace.
    disasm = re.sub('\s+', ' ', disasm)
    # Remove comments.
    disasm = re.sub('\s+#.*$', '', disasm)
    # gas accepts a ".s" suffix to indicate a non-canonical
    # reversed-operands encoding.  With "-M suffix", objdump prints
    # this.
    disasm = disasm.replace('.s ', ' ')
    if desc != disasm:
      print 'Mismatch (%i): %r != %r (%r) (%s)' % (
        index, desc, disasm, disasm_orig, ' '.join(bytes))