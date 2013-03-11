# Copyright (c) 2011 The Native Client Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import resource
import time

from memoize import Memoize
from trie import DftLabel, DftLabels
import objdump_check
import trie


def Byte(x):
  return '%02x' % x


regs64 = ('rax', 'rcx', 'rdx', 'rbx', 'rsp', 'rbp', 'rsi', 'rdi',
          'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15')
regs32 = ('eax', 'ecx', 'edx', 'ebx', 'esp', 'ebp', 'esi', 'edi',
          'r8d', 'r9d', 'r10d', 'r11d', 'r12d', 'r13d', 'r14d', 'r15d')
regs16 = ('ax', 'cx', 'dx', 'bx', 'sp', 'bp', 'si', 'di',
          'r8w', 'r9w', 'r10w', 'r11w', 'r12w', 'r13w', 'r14w', 'r15w')
regs_x87 = ['st(%i)' % regnum for regnum in range(8)]
regs_mmx = ['mm%i' % regnum for regnum in range(8)]
regs_xmm = ['xmm%i' % regnum for regnum in range(16)]

# 8-bit registers accessible with no REX prefix.
# These can be the low or high 8 bits of a 16-bit register.
regs8_original = ('al', 'cl', 'dl', 'bl', 'ah', 'ch', 'dh', 'bh')
# 8-bit registers accessible with a REX prefix.
# These are always the low 8 bits of a larger register.
regs8_extended = ('al', 'cl', 'dl', 'bl', 'spl', 'bpl', 'sil', 'dil',
                  'r8b', 'r9b', 'r10b', 'r11b', 'r12b', 'r13b', 'r14b', 'r15b')

nacl_unwritable_reg = set([
    'r15', 'r15d', 'r15w', 'r15b',
    'rsp', 'esp', 'sp', 'spl',
    'rbp', 'ebp', 'bp', 'bpl',
    ])

nacl_base_regs = ('r15', 'rsp', 'rbp')

regs_by_size = {
  64: regs64,
  32: regs32,
  16: regs16,
  'x87': regs_x87,
  'mmx': regs_mmx,
  'mmx32': regs_mmx,
  'mmx64': regs_mmx,
  'xmm': regs_xmm,
  'xmm32': regs_xmm,
  'xmm64': regs_xmm,
  }

def RegsBySize(has_rex, size):
  if size == 8:
    if has_rex:
      return regs8_extended
    else:
      return regs8_original
  else:
    return regs_by_size[size]

def GetExtendedRegs(top_bit, reglist):
  assert top_bit in (0, 1)
  assert len(reglist) in (8, 16)
  if len(reglist) == 16:
    reg_offset = top_bit << 3
  else:
    # This is used for MMX/x87 registers.
    reg_offset = 0
  for reg in xrange(8):
    yield reg, reglist[reg + reg_offset]

def GetOperandRegs(attrs, top_bit, reglist):
  # NaCl constraints
  for reg, regname in GetExtendedRegs(top_bit, reglist):
    reg_num = reg + (top_bit << 3)
    labels = []
    if attrs.canzeroextend and regname in ('esp', 'ebp'):
      labels.append(('requires_fixup', reg_num))
    elif not attrs.readonly and regname in nacl_unwritable_reg:
      continue
    elif attrs.canzeroextend and regname in regs32:
      labels.append(('zeroextends', reg_num))
    yield (reg, regname, labels)

mem_sizes = {
  128: 'OWORD PTR ',
  64: 'QWORD PTR ',
  32: 'DWORD PTR ',
  16: 'WORD PTR ',
  8: 'BYTE PTR ',
  'mmx32': 'DWORD PTR ',
  'mmx64': 'QWORD PTR ',
  'xmm': 'XMMWORD PTR ',
  'xmm32': 'DWORD PTR ',
  'xmm64': 'QWORD PTR ',
  'lea_mem': '',
  'prefetch_mem': 'BYTE PTR ',
  80: 'TBYTE PTR ',
  'other_x87_size': '',
  'fxsave_size': '',
  'lddqu_size': '', # Should be XMMWORD, but objdump omits this.
  }

# 'prefetch' instructions do not need to be sandboxed and can refer to
# addresses outside the sandbox's address space.
unsandboxed_mem = ('lea_mem', 'prefetch_mem')

cond_codes = (
  'o', 'no', 'b', 'ae', 'e', 'ne', 'be', 'a',
  's', 'ns', 'p', 'np', 'l', 'ge', 'le', 'g',
  )

form_map = {
    'Ib': ('imm', 8),
    'Gd': ('reg', 32),
    'Gq': ('reg', 64),
    'Ed': ('rm', 32),
    'Eq': ('rm', 64),
    'Md': ('mem', 32),
    'Mq': ('mem', 64),
    'Mdq': ('mem', 'xmm'),
    'Pd': ('reg', 'mmx'),
    'Pq': ('reg', 'mmx'),
    'Vd': ('reg', 'xmm32'),
    'Nq': ('reg2', 'mmx'),
    'Qd': ('rm', 'mmx32'),
    'Qq': ('rm', 'mmx64'),
    }

form_position_map = {
    'R': 'reg2',
    'U': 'reg2', # %xmm
    'V': 'reg', # %xmm
    'W': 'rm', # %xmm
    }

form_size_map = {
    'dq': 'xmm',   # XMMWORD
    'pd': 'xmm',   # XMMWORD
    'ps': 'xmm',   # XMMWORD
    'sd': 'xmm64', # QWORD
    'ss': 'xmm32', # DWORD
    'q': 'xmm64',  # QWORD
    }


time0 = time.time()
prev_time = time0

def Log(msg):
  global prev_time
  now = time.time()
  print '[+%.3fs] %.3fs: %s' % (now - prev_time, now - time0, str(msg))
  prev_time = now


def AssertEq(x, y):
  if x != y:
    raise AssertionError('%r != %r' % (x, y))


def CatBits(values, sizes_in_bits):
  result = 0
  for value, size_in_bits in zip(values, sizes_in_bits):
    assert isinstance(value, int)
    assert 0 <= value
    assert value < (1 << size_in_bits)
    result = (result << size_in_bits) | value
  return result


def CatBitsRev(value, sizes_in_bits):
  parts = []
  for size_in_bits in reversed(sizes_in_bits):
    parts.insert(0, value & ((1 << size_in_bits) - 1))
    value >>= size_in_bits
  AssertEq(value, 0)
  return tuple(parts)


@Memoize
def Sib(rex_x, rex_b, mod, rm_size, disp_size, disp_str, tail):
  nodes = []
  for index_reg, index_regname in GetExtendedRegs(rex_x, regs64):
    if index_reg == 4 and rex_x == 0:
      # %esp is not accepted in the position '(reg, %esp)'.
      # In this context, register 4 is %eiz (an always-zero value).
      index_regname = 'riz'
    for scale in (0, 1, 2, 3):
      # 5 is a special case and is not always %ebp.
      # %esi/%edi are missing from headings in table in doc.
      for base_reg, base_regname in GetExtendedRegs(rex_b, regs64):
        labels = []
        if index_regname == 'riz' and base_reg == 4 and scale == 0:
          index_result = ''
        else:
          index_result = '%s*%s' % (index_regname, 1 << scale)
          if rm_size not in unsandboxed_mem:
            labels.append(('requires_zeroextend', index_reg + (rex_x << 3)))
        if base_reg == 5 and mod == 0:
          base_regname = ''
          extra = 'VALUE32'
          disp_size2 = 4
        else:
          extra = ''
          disp_size2 = 0
        # XXX: NaCl constraint
        if (rm_size not in unsandboxed_mem
            and base_regname not in nacl_base_regs):
          continue
        parts = [base_regname, index_result, extra, disp_str]
        if (index_regname == 'riz' and base_reg == 5 and
            mod == 0 and scale == 0):
          desc = '%sds:VALUE32' % mem_sizes[rm_size]
        else:
          desc = FormatMemAccess(rm_size, parts)
        sib_byte = (scale << 6) | (index_reg << 3) | base_reg
        labels.append(('test_keep',
                       index_reg == 1 and scale == 0 and disp_size == 1))
        labels.append(('rm_arg', desc))
        nodes.append(
            TrieOfList(
                [Byte(sib_byte)],
                DftLabels(labels,
                          TrieOfList(['XX'] * (disp_size + disp_size2), tail))))
  return MergeMany(nodes, NoMerge)


def FormatMemAccess(size, parts):
  parts = [part for part in parts if part != '']
  return '%s[%s]' % (mem_sizes[size], '+'.join(parts))


@Memoize
def ModRMMem(rex_x, rex_b, rm_size, tail):
  got = []
  got.append((0, 5, TrieOfList(['XX'] * 4,
                               DftLabel('rm_arg',
                                        '%s[rip+VALUE32]' % mem_sizes[rm_size],
                                        tail))))
  for mod, dispsize, disp_str in ((0, 0, ''),
                                  (1, 1, 'VALUE8'),
                                  (2, 4, 'VALUE32')):
    for reg2, regname2 in GetExtendedRegs(rex_b, regs64):
      # XXX: NaCl constraint
      if rm_size not in unsandboxed_mem and regname2 not in nacl_base_regs:
        continue
      if reg2 == 4:
        # %esp is not accepted in this position.
        # 4 is a special value: adds SIB byte.
        continue
      if reg2 == 5 and mod == 0:
        continue
      got.append((mod, reg2,
                  TrieOfList(['XX'] * dispsize,
                             DftLabel('rm_arg',
                                      FormatMemAccess(rm_size,
                                                      [regname2, disp_str]),
                                      tail))))
    reg2 = 4
    got.append((mod, reg2, Sib(rex_x, rex_b, mod, rm_size,
                               dispsize, disp_str, tail)))
  return got


@Memoize
def ModRMReg(has_rex, rex_b, rm_size, rm_attrs, tail):
  got = []
  mod = 3
  for reg2, regname2, labels in GetOperandRegs(rm_attrs, rex_b,
                                               RegsBySize(has_rex, rm_size)):
    got.append((mod, reg2,
                DftLabels(labels,
                          DftLabel('test_keep', reg2 == 2 or len(labels) != 0,
                                   DftLabel('rm_arg', regname2, tail)))))
  return got


def ModRM1(has_rex, rex_x, rex_b, rm_size, rm_attrs,
           rm_allow_reg, rm_allow_mem, tail):
  if rm_allow_mem:
    for result in ModRMMem(rex_x, rex_b, rm_size, tail):
      yield result
  if rm_allow_reg:
    for result in ModRMReg(has_rex, rex_b, rm_size, rm_attrs, tail):
      yield result


def ModRM(has_rex, rex_r, rex_x, rex_b, reg_size, reg_attrs,
          rm_size, rm_attrs, rm_allow_reg, rm_allow_mem, tail):
  for reg, regname, labels in GetOperandRegs(reg_attrs, rex_r,
                                             RegsBySize(has_rex, reg_size)):
    for mod, reg2, node in ModRM1(has_rex, rex_x, rex_b, rm_size,
                                  rm_attrs, rm_allow_reg, rm_allow_mem, tail):
      yield TrieOfList([Byte((mod << 6) | (reg << 3) | reg2)],
                       DftLabels(labels,
                                 DftLabel('test_keep',
                                          reg == 3 or len(labels) != 0,
                                          DftLabel('reg_arg', regname, node))))


# Although the node this function returns won't get reused, the child
# nodes do get reused, which makes this worth memoizing.
@Memoize
def ModRMSingleArg(has_rex, rex_x, rex_b, rm_size, rm_attrs,
                   rm_allow_reg, rm_allow_mem, opcode, tail):
  nodes = []
  for mod, reg2, node in ModRM1(has_rex, rex_x, rex_b, rm_size,
                                rm_attrs, rm_allow_reg, rm_allow_mem, tail):
    nodes.append(TrieOfList([Byte((mod << 6) | (opcode << 3) | reg2)], node))
  return MergeMany(nodes, NoMerge)


def TrieNode(children, accept=False):
  node = trie.Trie()
  node.children = children
  node.accept = accept
  return node


def TrieOfList(bytes, node):
  for byte in reversed(bytes):
    node = TrieNode({byte: node})
  return node


# Assumes all the input nodes are immutable.
def MergeMany(nodes, merge_accept_types):
  if len(nodes) == 1:
    return list(nodes)[0]
  if len(nodes) == 0:
    return trie.EmptyNode
  children = {}
  accept_types = set()

  if isinstance(nodes[0], DftLabel):
    for node in nodes:
      if not isinstance(node, DftLabel):
        raise AssertionError('Not label, does not match %r' % nodes[0].key)
      AssertEq(node.key, nodes[0].key)
      AssertEq(node.value, nodes[0].value)
    return DftLabel(nodes[0].key,
                    nodes[0].value,
                    MergeMany([node.next for node in nodes],
                              merge_accept_types))

  by_key = {}
  for node in nodes:
    accept_types.add(node.accept)
    for key, value in node.children.iteritems():
      by_key.setdefault(key, []).append(value)
  for key, subnodes in by_key.iteritems():
    children[key] = MergeMany(subnodes, merge_accept_types)

  if len(accept_types) == 1:
    accept = list(accept_types)[0]
  else:
    accept = merge_accept_types(accept_types)
  return trie.MakeInterned(children, accept)


def TrieSize(start_node, expand_wildcards):
  @Memoize
  def Rec(node):
    if isinstance(node, DftLabel):
      return Rec(node.next)
    x = 0
    if node.accept:
      x += 1
    if expand_wildcards and 'XX' in node.children:
      return x + 256 * Rec(node.children['XX'])
    else:
      for child in node.children.itervalues():
        x += Rec(child)
      return x

  return Rec(start_node)


def TrieNodeCount(root):
  seen = set()
  def Rec(node):
    if node not in seen:
      seen.add(node)
      if isinstance(node, DftLabel):
        Rec(node.next)
      else:
        for child in node.children.itervalues():
          Rec(child)
  Rec(root)
  return len(seen)


def NoMerge(x):
  raise Exception('Cannot merge %r' % x)


@Memoize
def ImmediateNode(immediate_size):
  assert immediate_size in (0, 8, 16, 32, 64), immediate_size
  return TrieOfList(['XX'] * (immediate_size / 8), trie.AcceptNode)


@Memoize
def ModRMNode(has_rex, rex_r, rex_x, rex_b, reg_size, reg_attrs,
              rm_size, rm_attrs, rm_allow_reg, rm_allow_mem, tail):
  nodes = list(ModRM(has_rex, rex_r, rex_x, rex_b, reg_size, reg_attrs,
                     rm_size, rm_attrs, rm_allow_reg, rm_allow_mem, tail))
  return MergeMany(nodes, NoMerge)


# In cases where the instruction name and format depend on the
# contents of the ModRM byte, we need to apply the labels after the
# ModRM byte.
def PushLabels(labels, node):
  return TrieNode(dict((key, DftLabels(labels, value))
                       for key, value in node.children.iteritems()))


def FlattenTrie(node, bytes=[], labels=[]):
  if isinstance(node, DftLabel):
    for result in FlattenTrie(node.next, bytes, labels + [node]):
      yield result
  else:
    if node.accept:
      label_map = dict((label.key, label.value) for label in labels)
      yield (bytes, label_map)
    for byte, next in sorted(node.children.iteritems()):
      for result in FlattenTrie(next, bytes + [byte], labels):
        yield result


@Memoize
def StackFixup(reg):
  # This is the fixup instruction "addq %r15, %esp/%ebp".
  assert reg in (4, 5)
  return TrieOfList(map(Byte, [0x4c, 0x01, 0xf8 | reg]), trie.AcceptNode)


# Convert from a transducer (with labels) to an acceptor (no labels).
# Strip all labels, converting relative_jump labels into accept states.
@Memoize
def StripDftRec(node, accept_type, replace):
  if isinstance(node, DftLabel):
    if node.key == 'relative_jump':
      assert accept_type == 'normal_inst'
      accept_type = 'jump_rel%i' % node.value
    elif node.key == 'requires_fixup':
      assert accept_type == 'normal_inst'
      accept_type = 'replace'
      replace = StackFixup(node.value)
    new_node = StripDftRec(node.next, accept_type, replace)
    if node.key in ('requires_zeroextend', 'zeroextends'):
      # Keep the label
      new_node = trie.DftLabelInterned(node.key, node.value, new_node)
    return new_node
  else:
    assert node.accept in (True, False)
    if node.accept:
      if accept_type == 'replace':
        assert len(node.children) == 0
        return StripDft(replace)
      accept = accept_type
    else:
      accept = False
    return trie.MakeInterned(
        dict((key, StripDftRec(value, accept_type, replace))
             for key, value in node.children.iteritems()),
        accept)

def StripDft(node):
  return StripDftRec(node, 'normal_inst', None)


# Expand wildcard bytes.  This has two benefits:
#  * It allows wildcard edges to be merged with non-wildcards, in
#    order to support the 'superinst_start' case.
#  * It allows some nodes to be combined into one (combining explicit
#    and implicit wildcards).
@Memoize
def ExpandWildcards(node):
  if isinstance(node, DftLabel):
    return DftLabel(node.key, node.value, ExpandWildcards(node.next))
  if 'XX' in node.children:
    assert len(node.children) == 1, node.children.keys()
    dest = ExpandWildcards(node.children['XX'])
    children = dict((Byte(byte), dest) for byte in xrange(256))
  else:
    children = dict((key, ExpandWildcards(value))
                    for key, value in node.children.iteritems())
  return trie.MakeInterned(children, node.accept)


@Memoize
def FilterModRM(node):
  if isinstance(node, DftLabel):
    if node.key == 'test_keep' and not node.value:
      return trie.EmptyNode
    return DftLabel(node.key, node.value, FilterModRM(node.next))
  else:
    children = {}
    for key, value in node.children.iteritems():
      value = FilterModRM(value)
      if value != trie.EmptyNode:
        children[key] = value
    return TrieNode(children, node.accept)


def FilterPrefix(bytes, node):
  if len(bytes) == 0:
    return node
  elif isinstance(node, DftLabel):
    return DftLabel(node.key, node.value, FilterPrefix(bytes, node.next))
  else:
    next = FilterPrefix(bytes[1:], node.children.get(bytes[0], trie.EmptyNode))
    return TrieNode({bytes[0]: next}, node.accept)


def SubstSize(dec, size):
  def Subst(value):
    if value == 'imm8':
      return ('imm', 8)
    elif size == 64 and value in ('imm', 'jump_dest'):
      # Immediates are still just 32-bit even with REX.W set.
      return (value, 32)
    elif value == 'imm_movabs':
      # This is one immediate, however, that can be 64-bit.
      return ('imm', size)
    else:
      return (value, size)
  return map(Subst, dec)


# Instructions which can use the 'lock' prefix.
lock_whitelist = set([
    'adc', 'add', 'and', 'btc', 'btr', 'bts',
    'cmpxchg', 'cmpxchg8b', 'cmpxchg16b',
    'dec', 'inc',
    'neg', 'not', 'or', 'sbb', 'sub',
    'xadd', 'xchg', 'xor'])

# Instructions which we rely upon to zero the top 32 bits of the
# destination register.
zeroextend_whitelist = set([
    'mov',
    'movd', 'movsx', 'movsxd', 'movzx',
    'lea',
    'add', 'sub', 'xadd',
    # TODO: Original validator seems to reject "rex imul %esp" but not
    # "imul %esp".  Investigate that.
    # 'imul',
    'and', 'or', 'xor',
    'xchg',
    'neg', 'not'])


def SplitPrefixes(bytes):
  index = 0
  while bytes[index] in ('66', 'f2', 'f3'):
    index += 1
  return bytes[:index], bytes[index:]


def GetRexRoot(**kwargs):
  nodes = []
  for bytes, node in GetCoreRoot(has_rex=0, rex_w=0, rex_r=0, rex_x=0, rex_b=0,
                                 **kwargs):
    nodes.append(TrieOfList(bytes, node))
  for rex_bits in xrange(0x10):
    for bytes, node in GetCoreRoot(has_rex=1,
                                   rex_w=(rex_bits >> 3) & 1,
                                   rex_r=(rex_bits >> 2) & 1,
                                   rex_x=(rex_bits >> 1) & 1,
                                   rex_b=rex_bits & 1,
                                   **kwargs):
      prefixes, bytes = SplitPrefixes(bytes)
      nodes.append(TrieOfList(prefixes + [Byte(0x40 | rex_bits)],
                              DftLabel('test_keep', rex_bits in (0, 7, 8, 0xf),
                                       TrieOfList(bytes, node))))
  return MergeMany(nodes, NoMerge)


class OperandAttrs(object):
  pass

@Memoize
def MakeInternedAttrs(readonly, canzeroextend):
  attrs = OperandAttrs()
  attrs.readonly = readonly
  attrs.canzeroextend = canzeroextend
  return attrs

def AttrsFromKind(info):
  return MakeInternedAttrs(info.get('readonly', False),
                           info.get('canzeroextend', False))

def CoerceKind(kind):
  if isinstance(kind, str):
    return {"kind": kind}
  else:
    assert isinstance(kind, dict), kind
    return kind


def FixReg(reg_num, readonly=False):
  return {"kind": "fixreg", "reg_num": reg_num, "readonly": readonly}


def GetCoreRoot(has_rex, rex_w, rex_r, rex_x, rex_b, nacl_mode,
                mem_access_only=False, lockable_only=False,
                gs_access_only=False):
  top_nodes = []

  def Add(bytes, instr_name, args, modrm_opcode=None, data16=False):
    if instr_name == 'cmp':
      # Mark all operands as read-only.
      # TODO: Extend this to other instructions' operands.
      args = [({'kind': kind, 'readonly': True}, size) for kind, size in args]

    args = [(CoerceKind(kind), size) for kind, size in args]
    if lockable_only:
      if instr_name not in lock_whitelist:
        return
      dest_kind = args[0][0]['kind']
      if dest_kind not in ('rm', 'mem'):
        assert dest_kind in ('reg', '*ax', 'fixreg'), dest_kind
        return
    bytes = bytes.split()
    if nacl_mode:
      # The following restrictions are enforced by the original x86-32
      # NaCl validator, but might not be needed for safety.
      # %gs is allowed only with a limited set of instructions.
      # XXX: not for x86-64.
      if gs_access_only and (instr_name not in ('mov', 'cmp') or data16):
        return
      # Combining the data16 prefix with rep/repnz is not allowed.
      if data16 and bytes[0] in ('f2', 'f3'):
        return
      # repnz is not allowed with movs/stos, though that may just be a
      # mistake in the original validator.
      if instr_name in ('repnz movs', 'repnz stos'):
        return
      # These instructions are not allowed in their 16-bit forms.
      if data16 and instr_name in ('xadd', 'cmpxchg', 'shld', 'shrd',
                                   'bsf', 'bsr', 'jmp'):
        return

    immediate_size = 0 # Size in bits
    rm_size = None
    rm_attrs = None
    rm_allow_reg = not mem_access_only
    rm_allow_mem = True
    reg_size = None
    reg_attrs = None
    out_args = []
    labels = []
    mem_access = False

    def SimpleArg(arg):
      out_args.append((False, arg))

    if instr_name in zeroextend_whitelist:
      # Mark that the first operand can be zero-extended by the operation.
      arg = args[0][0].copy()
      arg['canzeroextend'] = True
      args = [(arg, args[0][1])] + args[1:]

    for kind_info, size in args:
      kind = kind_info['kind']
      if kind == 'imm':
        # We can have multiple immediates.  Needed for 'insertq'.
        immediate_size += size
        SimpleArg('VALUE%i' % size)
      elif kind == 'rm':
        assert rm_size is None
        rm_size = size
        rm_attrs = AttrsFromKind(kind_info)
        out_args.append((True, kind))
        mem_access = True
      elif kind == 'lea_mem':
        assert rm_size is None
        # For 'lea', the size is really irrelevant.
        rm_size = 'lea_mem'
        rm_allow_reg = False
        out_args.append((True, 'rm'))
      elif kind == 'mem':
        assert rm_size is None
        rm_size = size
        rm_allow_reg = False
        out_args.append((True, 'rm'))
        mem_access = True
      elif kind == 'reg2':
        # Register specified by the ModRM r/m field.  This is like the
        # 'rm' kind except that no memory access is allowed.
        assert rm_size is None
        rm_size = size
        rm_attrs = AttrsFromKind(kind_info)
        rm_allow_mem = False
        out_args.append((True, 'rm'))
      elif kind == 'reg':
        # Register specified by the ModRM reg field.
        assert reg_size is None
        reg_size = size
        reg_attrs = AttrsFromKind(kind_info)
        out_args.append((True, kind))
      elif kind == 'addr':
        # XXX: NaCl constraint
        return
        assert immediate_size == 0
        immediate_size = 64
        # We use mem_arg to allow 'ds:' to be replaced with 'gs:' later.
        out_args.append((True, 'mem'))
        labels.append(('mem_arg', 'ds:VALUE64'))
        mem_access = True
      elif kind == 'jump_dest':
        assert immediate_size == 0
        immediate_size = size
        SimpleArg('JUMP_DEST')
        labels.append(('relative_jump', size / 8))
      elif kind == '*ax':
        SimpleArg(RegsBySize(has_rex, size)[0])
      elif kind in ('1', 'cl', 'st'):
        SimpleArg(kind)
      elif kind == 'fixreg':
        regname = RegsBySize(has_rex, size)[kind_info["reg_num"] + (rex_b << 3)]
        # XXX: NaCl constraint
        if not kind_info['readonly'] and regname in nacl_unwritable_reg:
          return
        SimpleArg(regname)
      elif kind in ('es:[edi]', 'ds:[esi]'):
        SimpleArg(mem_sizes[size] + kind)
        # Although this accesses memory, we don't set 'mem_access = True'
        # because this cannot be used with lock/gs prefixes.
      else:
        raise AssertionError('Unknown arg type: %s' % repr(kind))

    if mem_access_only and not mem_access:
      return

    labels.append(('args', out_args))
    labels.append(('instr_name', instr_name))

    if rm_size is not None and reg_size is not None:
      assert modrm_opcode is None
      node = ModRMNode(has_rex, rex_r, rex_x, rex_b,
                       reg_size, reg_attrs,
                       rm_size, rm_attrs, rm_allow_reg, rm_allow_mem,
                       ImmediateNode(immediate_size))
      if not (rm_allow_reg and rm_allow_mem):
        node = PushLabels(labels, node)
        labels = []
    elif rm_size is not None and reg_size is None:
      assert modrm_opcode is not None
      node = ModRMSingleArg(has_rex, rex_x, rex_b, rm_size, rm_attrs,
                            rm_allow_reg, rm_allow_mem,
                            modrm_opcode, ImmediateNode(immediate_size))
      node = PushLabels(labels, node)
      labels = []
    elif rm_size is None and reg_size is None:
      assert modrm_opcode is None
      node = ImmediateNode(immediate_size)
    else:
      raise AssertionError('Unknown type')
    if data16:
      bytes = ['66'] + bytes
    top_nodes.append((bytes, DftLabels(labels, node)))

  def Add3DNow(instrs):
    # AMD 3DNow instructions are treated specially because the 3DNow
    # opcode is placed at the end of the instruction, in the position
    # where immediate values are normally placed.
    if lockable_only:
      return
    if nacl_mode and gs_access_only:
      return
    node = TrieNode(dict((Byte(imm_opcode),
                          DftLabel('instr_name', name, trie.AcceptNode))
                         for imm_opcode, name in instrs))
    rm_allow_reg = not mem_access_only
    rm_allow_mem = True
    node = DftLabel('args', [(True, 'reg'), (True, 'rm')],
                    ModRMNode(has_rex, rex_r, rex_b, rex_b,
                              'mmx', AttrsFromKind({}),
                              'mmx64', AttrsFromKind({}),
                              rm_allow_reg, rm_allow_mem, node))
    top_nodes.append((['0f', '0f'], node))

  def AddFPMem(bytes, instr_name, modrm_opcode, size=32):
    Add(bytes, instr_name, [('mem', size)], modrm_opcode=modrm_opcode)

  x87_formats = {
      'st reg': [('st', 'x87'), ('reg2', 'x87')],
      'reg st': [('reg2', 'x87'), ('st', 'x87')],
      'reg': [('reg2', 'x87')],
      }

  def AddFPReg(bytes, instr_name, modrm_opcode, format='st reg'):
    Add(bytes, instr_name, x87_formats[format], modrm_opcode=modrm_opcode)

  def AddFPRM(bytes, instr_name, modrm_opcode, format='st reg', size=32):
    AddFPMem(bytes, instr_name, modrm_opcode, size)
    AddFPReg(bytes, instr_name, modrm_opcode, format)

  def RexSize(size):
    if rex_w:
      return 64
    else:
      return size

  def AddLW(opcode, instr, format, **kwargs):
    Add(Byte(opcode), instr, SubstSize(format, RexSize(16)),
        data16=True, **kwargs)
    Add(Byte(opcode), instr, SubstSize(format, RexSize(32)), **kwargs)

  # Like AddLW(), but takes a string rather than an int.
  # TODO: Unify these.
  def AddLW2(opcode, instr, format, **kwargs):
    Add(opcode, instr, SubstSize(format, RexSize(16)), data16=True, **kwargs)
    Add(opcode, instr, SubstSize(format, RexSize(32)), **kwargs)

  # Like AddLW(), but 'push' and 'pop' never use a 32-bit operand.
  # They use a 64-bit operand even without a REX.W prefix.
  def AddLWPushPop(opcode, instr, format, **kwargs):
    Add(Byte(opcode), instr, SubstSize(format, RexSize(16)),
        data16=True, **kwargs)
    Add(Byte(opcode), instr, SubstSize(format, 64), **kwargs)

  def AddPair(opcode, instr, format, **kwargs):
    Add(Byte(opcode), instr, SubstSize(format, 8), **kwargs)
    AddLW(opcode + 1, instr, format, **kwargs)

  # Like AddPair(), but also takes a prefix.
  def AddPair2(prefix, opcode, instr, format, **kwargs):
    Add(prefix + ' ' + Byte(opcode), instr, SubstSize(format, 8), **kwargs)
    AddLW2(prefix + ' ' + Byte(opcode + 1), instr, format, **kwargs)

  def AddForm(bytes, instr_name, format, modrm_opcode=None):
    def MapArg(arg):
      if arg in form_map:
        return form_map[arg]
      elif arg[1:] == 'd/q':
        if arg[0] == 'E':
          kind = 'rm'
        elif arg[0] == 'G':
          kind = 'reg'
        else:
          raise AssertionError('Bad d/q kind')
        if rex_w:
          size = 64
        else:
          size = 32
        return (kind, size)
      else:
        return (form_position_map[arg[0]], form_size_map[arg[1:]])
    Add(bytes, instr_name, map(MapArg, format.split()),
        modrm_opcode=modrm_opcode)

  def AddSSEMMXPair(opcode, name):
    AddForm(opcode, name, 'Pq Qq')
    AddForm('66 ' + opcode, name, 'Vdq Wdq')

  # Arithmetic instructions
  for arith_opcode, instr in enumerate(['add', 'or', 'adc', 'sbb',
                                        'and', 'sub', 'xor', 'cmp']):
    for format_num, format in enumerate([['rm', 'reg'],
                                         ['reg', 'rm'],
                                         ['*ax', 'imm']]):
      opcode = CatBits([arith_opcode, format_num, 0], [5, 2, 1])
      AddPair(opcode, instr, format)
    # Group 1
    AddPair(0x80, instr, ['rm', 'imm'], modrm_opcode=arith_opcode)
    # 0x82 is a hole in the table.  We don't use AddPair(0x82) here
    # because 0x80 and 0x82 would be equivalent (both 8-bit ops with
    # imm8).
    AddLW(0x83, instr, ['rm', 'imm8'], modrm_opcode=arith_opcode)

  # Group 2: shift instructions
  for instr, modrm_opcode in [('rol', 0),
                              ('ror', 1),
                              ('rcl', 2),
                              ('rcr', 3),
                              ('shl', 4),
                              ('shr', 5),
                              # 6 is absent.
                              ('sar', 7),
                              ]:
    AddPair(0xc0, instr, ['rm', 'imm8'], modrm_opcode=modrm_opcode)
    AddPair(0xd0, instr, ['rm', '1'], modrm_opcode=modrm_opcode)
    AddPair(0xd2, instr, ['rm', 'cl'], modrm_opcode=modrm_opcode)

  for reg_num in range(8):
    # Not for x86-64.  These bytes are used for the REX prefixes instead.
    # AddLW(0x40 + reg_num, 'inc', [('fixreg', reg_num)])
    # AddLW(0x48 + reg_num, 'dec', [('fixreg', reg_num)])
    AddLWPushPop(0x50 + reg_num, 'push', [FixReg(reg_num, readonly=True)])
    AddLWPushPop(0x58 + reg_num, 'pop', [FixReg(reg_num)])

  # These 'push' instructions all move %rsp by 8 bytes.  In binutils
  # 2.20.1, objdump incorrectly decodes "66 68" as having a following
  # 32-bit immediate, when it really has a 16-bit immediate.  This is
  # fixed in newer a binutils version.
  # With this fixed, the next two lines can be replaced by:
  #   AddLWPushPop(0x68, 'push', ['imm'])
  Add('68', 'push', [('imm', 32)])
  # Add('68', 'FIXME push', [('imm', 16)], data16=True)
  Add('6a', 'push', [('imm', 8)])
  # This moves %rsp by 2 bytes.
  # The original x86-64 validator does not allow this although the
  # original x86-32 validator does.
  # TODO: does not seem to be valid in x86-64.
  # if not nacl_mode:
  #   Add('66 6a', 'push', [('imm', 8)])

  AddLW(0x69, 'imul', ['reg', 'rm', 'imm'])
  AddLW(0x6b, 'imul', ['reg', 'rm', 'imm8'])

  # Short (8-bit offset) conditional jumps
  for cond_num, cond_name in enumerate(cond_codes):
    Add(Byte(0x70 + cond_num), 'j' + cond_name, [('jump_dest', 8)])

  AddPair(0x84, 'test', ['rm', 'reg'])
  AddPair(0x86, 'xchg', ['rm', 'reg'])
  AddLW(0x8d, 'lea', ['reg', 'lea_mem'])
  # Group 1a just contains 'pop'.
  AddLWPushPop(0x8f, 'pop', ['rm'], modrm_opcode=0)

  if not has_rex:
    # 'nop' is really 'xchg %eax, %eax'.
    Add('90', 'nop', [])
    # This might also be called 'data16 nop'.
    Add('66 90', 'xchg ax, ax', [])
    # 'pause' is really 'rep nop'.
    Add('f3 90', 'pause', [])
  # TODO: Could allow '48 90' (rex.W nop)
  for reg_num in range(8):
    if reg_num != 0:
      AddLW(0x90 + reg_num, 'xchg', [FixReg(reg_num), '*ax'])

  if rex_w:
    # "Convert long to quad".  Sign-extends %ax into %eax.
    Add('98', 'cdqe', [])
  else:
    # "Convert word to long".  Sign-extends %ax into %eax.
    Add('98', 'cwde', [])
    # "Convert byte to word".  Sign-extends %al into %ax.
    Add('66 98', 'cbw', [])
  if rex_w:
    # "Convert quad to double quad".  Fills %rdx with the top bit of %rax.
    Add('99', 'cqo', [])
  else:
    # "Convert long to double long".  Fills %edx with the top bit of %eax.
    Add('99', 'cdq', [])
    # "Convert word to double word".  Fills %dx with the top bit of %ax.
    Add('66 99', 'cwd', [])
  # Note that assemblers and disassemblers treat 'fwait' as a prefix
  # such that 'fwait; fnXXX' is a shorthand for 'fXXX'.  (For example,
  # 'fwait; fnstenv ARG' can be written as 'fstenv ARG'.)  This might
  # cause cross-check tests to fail if these instructions are placed
  # together.  Really, though, fwait is an instruction in its own
  # right.
  # TODO: Accept a REX prefix on fwait to match the original validator?
  if not has_rex:
    Add('9b', 'fwait', [])
  # NaCl does not allow 'sahf' and 'lahf' on x86-64.
  # Add('9e', 'sahf', [])
  # Add('9f', 'lahf', [])
  Add('f4', 'hlt', [])

  if not nacl_mode:
    # Not valid for x86-64.
    #Add('27', 'daa', [])
    #Add('2f', 'das', [])
    #Add('37', 'aaa', [])
    #Add('3f', 'aas', [])
    #Add('60', 'pusha', [])
    #Add('61', 'popa', [])
    Add('9c', 'pushf', [])
    Add('9d', 'popf', [])
    Add('c2', 'ret', [('imm', 16)])
    Add('c3', 'ret', [])
    Add('cc', 'int3', [])
    Add('cd', 'int', [('imm', 8)])
    # Not valid for x86-64.
    #Add('ce', 'into', [])
    Add('cf', 'iret', [])
    Add('fa', 'cli', [])
    Add('fb', 'sti', [])

  # x86-64 NaCl does not allow 'leave' because it modifies the top 32
  # bits of %rbp.
  # Add('c9', 'leave', [])
  # # 'data16 leave' is probably never useful, but we allow it for
  # # consistency with the original NaCl x86-32 validator.
  # # See http://code.google.com/p/nativeclient/issues/detail?id=2244
  # Add('66 c9', 'data16 leave', [])

  Add('e8', 'call', [('jump_dest', 32)])

  # # String operations.
  # for prefix_bytes, prefix in [('', ''),
  #                              ('f2', 'repnz '),
  #                              ('f3', 'rep ')]:
  #   AddPair2(prefix_bytes, 0xa4, prefix + 'movs', ['es:[edi]', 'ds:[esi]'])
  #   AddPair2(prefix_bytes, 0xaa, prefix + 'stos', ['es:[edi]', '*ax'])
  #   if not nacl_mode:
  #     AddPair2(prefix_bytes, 0xac, prefix + 'lods', ['*ax', 'ds:[esi]'])
  # for prefix_bytes, prefix in [('', ''),
  #                              ('f2', 'repnz '),
  #                              ('f3', 'repz ')]:
  #   AddPair2(prefix_bytes, 0xa6, prefix + 'cmps', ['ds:[esi]', 'es:[edi]'])
  #   AddPair2(prefix_bytes, 0xae, prefix + 'scas', ['*ax', 'es:[edi]'])

  AddPair(0xa8, 'test', ['*ax', 'imm'])

  if not nacl_mode:
    Add('e0', 'loopne', [('jump_dest', 8)])
    Add('e1', 'loope', [('jump_dest', 8)])
    Add('e2', 'loop', [('jump_dest', 8)])
    if not has_rex:
      Add('e3', 'jrcxz', [('jump_dest', 8)])
      Add('67 e3', 'jecxz', [('jump_dest', 8)])
  Add('e9', 'jmp', [('jump_dest', 32)])
  Add('eb', 'jmp', [('jump_dest', 8)])

  Add('f5', 'cmc', []) # Complement carry flag
  Add('f8', 'clc', []) # Clear carry flag
  Add('f9', 'stc', []) # Set carry flag
  Add('fc', 'cld', []) # Clear direction flag
  Add('fd', 'std', []) # Set direction flag

  # Group 3
  AddPair(0xf6, 'test', ['rm', 'imm'], modrm_opcode=0)
  for instr, modrm_opcode in [('not', 2),
                              ('neg', 3),
                              ('mul', 4),
                              ('imul', 5),
                              ('div', 6),
                              ('idiv', 7)]:
    AddPair(0xf6, instr, ['rm'], modrm_opcode=modrm_opcode)

  # Group 4/5
  AddPair(0xfe, 'inc', ['rm'], modrm_opcode=0)
  AddPair(0xfe, 'dec', ['rm'], modrm_opcode=1)
  # Group 5
  AddLWPushPop(0xff, 'push', ['rm'], modrm_opcode=6)
  # NaCl disallows using these without a mask instruction first.
  # Note that allowing jmp/call with a data16 prefix isn't very useful.
  if not nacl_mode:
    Add('ff', 'call', [('rm', 64)], modrm_opcode=2)
    Add('ff', 'jmp', [('rm', 64)], modrm_opcode=4)

  AddPair(0x88, 'mov', ['rm', {'kind': 'reg', 'readonly': True}])
  AddPair(0x8a, 'mov', ['reg', 'rm'])
  AddPair(0xc6, 'mov', ['rm', 'imm'], modrm_opcode=0) # Group 11
  AddPair(0xa0, 'mov', ['*ax', 'addr'])
  AddPair(0xa2, 'mov', ['addr', '*ax'])
  for reg_num in range(8):
    Add(Byte(0xb0 + reg_num), 'mov', [(FixReg(reg_num), 8), ('imm', 8)])
    AddLW(0xb8 + reg_num, 'mov', [FixReg(reg_num), 'imm_movabs'])

  # Two-byte opcodes.

  if not nacl_mode:
    Add('0f 05', 'syscall', [])
    Add('0f 06', 'clts', [])
    Add('0f 07', 'sysret', [])
    Add('0f 08', 'invd', [])
    Add('0f 09', 'wbinvd', [])
    Add('0f 0b', 'ud2', [])
    Add('0f 01 d8', 'vmrun', [])
    Add('0f 01 d9', 'vmmcall', [])
    Add('0f 01 da', 'vmload', [])
    Add('0f 01 db', 'vmsave', [])
    Add('0f 01 dc', 'stgi', [])
    Add('0f 01 dd', 'clgi', [])
    Add('0f 01 de', 'skinit', [])
    Add('0f 01 df', 'invlpga', [])
    # 'swapgs' is 64-bit-only.
    # Add('0f 01 f8', 'swapgs', [])
    Add('0f 01 f9', 'rdtscp', [])
  Add('0f 0e', 'femms', [])
  # Group P: prefetches
  # TODO: Other modrm_opcode values for prefetches might be allowed.
  Add('0f 0d', 'prefetch', [('mem', 'prefetch_mem')], modrm_opcode=0)
  Add('0f 0d', 'prefetchw', [('mem', 'prefetch_mem')], modrm_opcode=1)

  Add('0f 10', 'movups', [('reg', 'xmm'), ('rm', 'xmm')])
  Add('0f 11', 'movups', [('rm', 'xmm'), ('reg', 'xmm')])
  Add('0f 12', 'movlps', [('reg', 'xmm'), ('mem', 64)])
  Add('0f 12', 'movhlps', [('reg', 'xmm'), ('reg2', 'xmm')])
  Add('0f 13', 'movlps', [('mem', 64), ('reg', 'xmm')])
  Add('0f 14', 'unpcklps', [('reg', 'xmm'), ('rm', 'xmm')])
  Add('0f 15', 'unpckhps', [('reg', 'xmm'), ('rm', 'xmm')])
  Add('0f 16', 'movhps', [('reg', 'xmm'), ('mem', 64)])
  Add('0f 16', 'movlhps', [('reg', 'xmm'), ('reg2', 'xmm')])
  Add('0f 17', 'movhps', [('mem', 64), ('reg', 'xmm')])
  # Group 16
  Add('0f 18', 'prefetchnta', [('mem', 'prefetch_mem')], modrm_opcode=0)
  Add('0f 18', 'prefetcht0', [('mem', 'prefetch_mem')], modrm_opcode=1)
  Add('0f 18', 'prefetcht1', [('mem', 'prefetch_mem')], modrm_opcode=2)
  Add('0f 18', 'prefetcht2', [('mem', 'prefetch_mem')], modrm_opcode=3)

  Add('f3 0f 10', 'movss', [('reg', 'xmm'), ('rm', 'xmm32')])
  Add('f3 0f 11', 'movss', [('rm', 'xmm32'), ('reg', 'xmm')])
  Add('f3 0f 12', 'movsldup', [('reg', 'xmm'), ('rm', 'xmm')])
  Add('f3 0f 16', 'movshdup', [('reg', 'xmm'), ('rm', 'xmm')])

  Add('66 0f 10', 'movupd', [('reg', 'xmm'), ('rm', 'xmm')])
  Add('66 0f 11', 'movupd', [('rm', 'xmm'), ('reg', 'xmm')])
  Add('66 0f 12', 'movlpd', [('reg', 'xmm'), ('mem', 64)])
  Add('66 0f 13', 'movlpd', [('mem', 64), ('reg', 'xmm')])
  Add('66 0f 14', 'unpcklpd', [('reg', 'xmm'), ('rm', 'xmm')])
  Add('66 0f 15', 'unpckhpd', [('reg', 'xmm'), ('rm', 'xmm')])
  Add('66 0f 16', 'movhpd', [('reg', 'xmm'), ('mem', 64)])
  Add('66 0f 17', 'movhpd', [('mem', 64), ('reg', 'xmm')])

  Add('f2 0f 10', 'movsd', [('reg', 'xmm'), ('rm', 'xmm64')])
  Add('f2 0f 11', 'movsd', [('rm', 'xmm64'), ('reg', 'xmm')])
  Add('f2 0f 12', 'movddup', [('reg', 'xmm'), ('rm', 'xmm64')])

  # Skip 0f 2x ('mov' on control registers)
  AddForm('0f 28', 'movaps', 'Vps Wps')
  AddForm('0f 29', 'movaps', 'Wps Vps')
  AddForm('66 0f 28', 'movapd', 'Vpd Wpd')
  AddForm('66 0f 29', 'movapd', 'Wpd Vpd')
  AddForm('0f 2a', 'cvtpi2ps', 'Vps Qq')
  AddForm('f3 0f 2a', 'cvtsi2ss', 'Vss Ed/q')
  AddForm('66 0f 2a', 'cvtpi2pd', 'Vpd Qq')
  AddForm('f2 0f 2a', 'cvtsi2sd', 'Vsd Ed/q')
  AddForm('0f 2b', 'movntps', 'Mdq Vps')
  AddForm('f3 0f 2b', 'movntss', 'Md Vss')
  AddForm('66 0f 2b', 'movntpd', 'Mdq Vpd')
  AddForm('f2 0f 2b', 'movntsd', 'Mq Vsd')
  # binutils correctly disassembles 'cvttps2pi' with 'QWORD PTR', but
  # the assembler wrongly only accepts 'XMMWORD PTR'.
  # The AMD manual has 'Pq Wps' for 'cvttps2pi', but 'W' is wrong (it
  # should be an MMX register, not an XMM register) and 'ps' is wrong
  # (it should be 64-bit, not 128-bit).
  Add('0f 2c', 'FIXME cvttps2pi', [('reg', 'mmx'), ('rm', 'xmm64')])
  AddForm('f3 0f 2c', 'cvttss2si', 'Gd/q Wss')
  AddForm('66 0f 2c', 'cvttpd2pi', 'Pq Wpd')
  AddForm('f2 0f 2c', 'cvttsd2si', 'Gd/q Wsd')
  Add('0f 2d', 'cvtps2pi', [('reg', 'mmx'), ('rm', 'xmm64')])
  AddForm('f3 0f 2d', 'cvtss2si', 'Gd/q Wss')
  AddForm('66 0f 2d', 'cvtpd2pi', 'Pq Wpd')
  AddForm('f2 0f 2d', 'cvtsd2si', 'Gd/q Wsd')
  AddForm('0f 2e', 'ucomiss', 'Vss Wss')
  AddForm('66 0f 2e', 'ucomisd', 'Vsd Wsd')
  # The AMD manual uses 'Vps Wps', but 'ps' is not correct because
  # this writes to a 32-bit memory location.
  Add('0f 2f', 'comiss', [('reg', 'xmm'), ('rm', 'xmm32')])
  AddForm('66 0f 2f', 'comisd', 'Vpd Wsd')

  Add('0f 31', 'rdtsc', [])
  if not nacl_mode:
    Add('0f 30', 'wrmsr', [])
    Add('0f 32', 'rdmsr', [])
    Add('0f 33', 'rdpmc', [])
    Add('0f 34', 'sysenter', [])
    Add('0f 35', 'sysexit', [])

  # AddForm('0f 50', 'movmskps', 'Gd Ups')
  AddForm('0f 51', 'sqrtps', 'Vps Wps')
  AddForm('0f 52', 'rsqrtps', 'Vps Rps')
  AddForm('0f 53', 'rcpps', 'Vps Wps')
  AddForm('0f 54', 'andps', 'Vps Wps')
  AddForm('0f 55', 'andnps', 'Vps Wps')
  AddForm('0f 56', 'orps', 'Vps Wps')
  AddForm('0f 57', 'xorps', 'Vps Wps')
  AddForm('f3 0f 51', 'sqrtss', 'Vss Wss')
  AddForm('f3 0f 52', 'rsqrtss', 'Vss Wss')
  AddForm('f3 0f 53', 'rcpss', 'Vss Wss')
  # AddForm('66 0f 50', 'movmskpd', 'Gd Upd')
  AddForm('66 0f 51', 'sqrtpd', 'Vpd Wpd')
  AddForm('66 0f 54', 'andpd', 'Vpd Wpd')
  AddForm('66 0f 55', 'andnpd', 'Vpd Wpd')
  AddForm('66 0f 56', 'orpd', 'Vpd Wpd')
  AddForm('66 0f 57', 'xorpd', 'Vpd Wpd')
  AddForm('f2 0f 51', 'sqrtsd', 'Vsd Wsd')

  for opcode, name in [('0f 58', 'add'),
                       ('0f 59', 'mul'),
                       ('0f 5c', 'sub'),
                       ('0f 5d', 'min'),
                       ('0f 5e', 'div'),
                       ('0f 5f', 'max')]:
    AddForm(opcode, name + 'ps', 'Vps Wps')
    AddForm('f3 ' + opcode, name + 'ss', 'Vss Wss')
    AddForm('66 ' + opcode, name + 'pd', 'Vpd Wpd')
    AddForm('f2 ' + opcode, name + 'sd', 'Vsd Wsd')
  # The AMD manual has 'Vpd Wps', but 'Wps' is not correct because the
  # operand is 64-bit.
  Add('0f 5a', 'cvtps2pd', [('reg', 'xmm'), ('rm', 'xmm64')])
  AddForm('f3 0f 5a', 'cvtss2sd', 'Vsd Wss')
  AddForm('66 0f 5a', 'cvtpd2ps', 'Vps Wpd')
  AddForm('f2 0f 5a', 'cvtsd2ss', 'Vss Wsd')
  AddForm('0f 5b', 'cvtdq2ps', 'Vps Wdq')
  AddForm('f3 0f 5b', 'cvttps2dq', 'Vdq Wps')
  AddForm('66 0f 5b', 'cvtps2dq', 'Vdq Wps')
  # 'f3 0f 5b' is invalid.

  # MMX
  AddForm('0f 60', 'punpcklbw', 'Pq Qd')
  AddForm('0f 61', 'punpcklwd', 'Pq Qd')
  AddForm('0f 62', 'punpckldq', 'Pq Qd')
  AddForm('0f 63', 'packsswb', 'Pq Qq')
  AddForm('0f 64', 'pcmpgtb', 'Pq Qq')
  AddForm('0f 65', 'pcmpgtw', 'Pq Qq')
  AddForm('0f 66', 'pcmpgtd', 'Pq Qq')
  AddForm('0f 67', 'packuswb', 'Pq Qq')

  # SSE
  # The AMD manual says 'Wq' rather than 'Wdq' for the next three, but
  # it seems to be wrong.
  AddForm('66 0f 60', 'punpcklbw', 'Vdq Wdq')
  AddForm('66 0f 61', 'punpcklwd', 'Vdq Wdq')
  AddForm('66 0f 62', 'punpckldq', 'Vdq Wdq')
  AddForm('66 0f 63', 'packsswb', 'Vdq Wdq')
  AddForm('66 0f 64', 'pcmpgtb', 'Vdq Wdq')
  AddForm('66 0f 65', 'pcmpgtw', 'Vdq Wdq')
  AddForm('66 0f 66', 'pcmpgtd', 'Vdq Wdq')
  AddForm('66 0f 67', 'packuswb', 'Vdq Wdq')

  AddSSEMMXPair('0f 68', 'punpckhbw')
  AddSSEMMXPair('0f 69', 'punpckhwd')
  AddSSEMMXPair('0f 6a', 'punpckhdq')
  AddSSEMMXPair('0f 6b', 'packssdw')
  # The AMD manual says 'Wq' rather than 'Wdq' for punpcklqdq and
  # punpckhqdq, but it seems to be wrong.
  AddForm('66 0f 6c', 'punpcklqdq', 'Vdq Wdq')
  AddForm('66 0f 6d', 'punpckhqdq', 'Vdq Wdq')
  # d/q switch
  if rex_w:
    AddForm('0f 6e', 'movq', 'Pq Eq')
    AddForm('66 0f 6e', 'movq', 'Vdq Eq')
  else:
    AddForm('0f 6e', 'movd', 'Pq Ed')
    AddForm('66 0f 6e', 'movd', 'Vdq Ed')
  AddForm('0f 6f', 'movq', 'Pq Qq')
  AddForm('f3 0f 6f', 'movdqu', 'Vdq Wdq')
  AddForm('66 0f 6f', 'movdqa', 'Vdq Wdq')

  # The AMD manual says 'Wq' rather than 'Wdq' for pshufhw and
  # pshuflw, but it seems to be wrong.
  AddForm('0f 70', 'pshufw', 'Pq Qq Ib')
  AddForm('f3 0f 70', 'pshufhw', 'Vq Wdq Ib')
  AddForm('66 0f 70', 'pshufd', 'Vdq Wdq Ib')
  AddForm('f2 0f 70', 'pshuflw', 'Vq Wdq Ib')
  AddForm('0f 74', 'pcmpeqb', 'Pq Qq')
  AddForm('0f 75', 'pcmpeqw', 'Pq Qq')
  AddForm('0f 76', 'pcmpeqd', 'Pq Qq')
  AddForm('0f 77', 'emms', '')
  AddForm('66 0f 74', 'pcmpeqb', 'Vdq Wdq')
  AddForm('66 0f 75', 'pcmpeqw', 'Vdq Wdq')
  AddForm('66 0f 76', 'pcmpeqd', 'Vdq Wdq')
  AddForm('f2 0f 78', 'insertq', 'Vdq Uq Ib Ib')
  AddForm('66 0f 79', 'extrq', 'Vdq Uq')
  AddForm('f2 0f 79', 'insertq', 'Vdq Udq')
  AddForm('66 0f 7c', 'haddpd', 'Vpd Wpd')
  AddForm('f2 0f 7c', 'haddps', 'Vps Wps')
  AddForm('66 0f 7d', 'hsubpd', 'Vpd Wpd')
  AddForm('f2 0f 7d', 'hsubps', 'Vps Wps')
  AddForm('f3 0f 7e', 'movq', 'Vq Wq')
  if rex_w:
    AddForm('0f 7e', 'movq', 'Eq Pq') # Ed/q Pd/q
    AddForm('66 0f 7e', 'movq', 'Eq Vq') # Ed/q Vd/q
  else:
    AddForm('0f 7e', 'movd', 'Ed Pd') # Ed/q Pd/q
    AddForm('66 0f 7e', 'movd', 'Ed Vd') # Ed/q Vd/q
  AddForm('0f 7f', 'movq', 'Qq Pq')
  AddForm('f3 0f 7f', 'movdqu', 'Wdq Vdq')
  AddForm('66 0f 7f', 'movdqa', 'Wdq Vdq')
  # Group 12
  AddForm('0f 71', 'psrlw', 'Nq Ib', modrm_opcode=2)
  AddForm('0f 71', 'psraw', 'Nq Ib', modrm_opcode=4)
  AddForm('0f 71', 'psllw', 'Nq Ib', modrm_opcode=6)
  AddForm('66 0f 71', 'psrlw', 'Udq Ib', modrm_opcode=2)
  AddForm('66 0f 71', 'psraw', 'Udq Ib', modrm_opcode=4)
  AddForm('66 0f 71', 'psllw', 'Udq Ib', modrm_opcode=6)
  # Group 13
  AddForm('0f 72', 'psrld', 'Nq Ib', modrm_opcode=2)
  AddForm('0f 72', 'psrad', 'Nq Ib', modrm_opcode=4)
  AddForm('0f 72', 'pslld', 'Nq Ib', modrm_opcode=6)
  AddForm('66 0f 72', 'psrld', 'Udq Ib', modrm_opcode=2)
  AddForm('66 0f 72', 'psrad', 'Udq Ib', modrm_opcode=4)
  AddForm('66 0f 72', 'pslld', 'Udq Ib', modrm_opcode=6)
  # Group 14
  AddForm('0f 73', 'psrlq', 'Nq Ib', modrm_opcode=2)
  AddForm('0f 73', 'psllq', 'Nq Ib', modrm_opcode=6)
  AddForm('66 0f 73', 'psrlq', 'Udq Ib', modrm_opcode=2)
  AddForm('66 0f 73', 'psrldq', 'Udq Ib', modrm_opcode=3)
  AddForm('66 0f 73', 'psllq', 'Udq Ib', modrm_opcode=6)
  AddForm('66 0f 73', 'pslldq', 'Udq Ib', modrm_opcode=7)
  # Group 17
  if not nacl_mode:
    # The AMD manual says 'Vdq' (reg), but it should be 'Udq' (reg2).
    # This form of extrq is disallowed.
    # See http://code.google.com/p/nativeclient/issues/detail?id=1970
    AddForm('66 0f 78', 'extrq', 'Udq Ib Ib', modrm_opcode=0)

  for cond_num, cond_name in enumerate(cond_codes):
    # Conditional move.  Added in P6.
    AddLW2('0f ' + Byte(0x40 + cond_num), 'cmov' + cond_name,
           ['reg', {'kind': 'rm', 'readonly': True}])
    # 4-byte offset jumps.
    Add('0f ' + Byte(0x80 + cond_num), 'j' + cond_name, [('jump_dest', 32)])
    # 2-byte offset jumps. Not for x86-64 mode.
    # if not nacl_mode:
    #   Add('66 0f ' + Byte(0x80 + cond_num), 'j' + cond_name,
    #       [('jump_dest', 32)])
    # Byte set on condition
    Add('0f ' + Byte(0x90 + cond_num), 'set' + cond_name, [('rm', 8)],
        modrm_opcode=0)

  Add('0f a2', 'cpuid', [])
  if not nacl_mode:
    # Bit test/set/clear operations
    AddLW2('0f a3', 'bt', ['rm', 'reg'])
    AddLW2('0f ab', 'bts', ['rm', 'reg'])
    AddLW2('0f b3', 'btr', ['rm', 'reg'])
    AddLW2('0f bb', 'btc', ['rm', 'reg'])
    # Group 8
    AddLW2('0f ba', 'bt', ['rm', 'imm8'], modrm_opcode=4)
    AddLW2('0f ba', 'bts', ['rm', 'imm8'], modrm_opcode=5)
    AddLW2('0f ba', 'btr', ['rm', 'imm8'], modrm_opcode=6)
    AddLW2('0f ba', 'btc', ['rm', 'imm8'], modrm_opcode=7)

  # Bit shift left/right
  AddLW2('0f a4', 'shld', ['rm', 'reg', 'imm8'])
  AddLW2('0f a5', 'shld', ['rm', 'reg', 'cl'])
  AddLW2('0f ac', 'shrd', ['rm', 'reg', 'imm8'])
  AddLW2('0f ad', 'shrd', ['rm', 'reg', 'cl'])

  if not nacl_mode:
    Add('0f aa', 'rsm', [])
  AddLW2('0f af', 'imul', ['reg', 'rm'])

  # Bit scan forwards/reverse
  AddLW2('0f bc', 'bsf', ['reg', 'rm'])
  AddLW2('0f bd', 'bsr', ['reg', 'rm'])

  # Move with zero/sign extend.
  if rex_w:
    Add('0f b6', 'movzx', [('reg', 64), ('rm', 8)])
    Add('0f b7', 'movzx', [('reg', 64), ('rm', 16)])
    Add('0f be', 'movsx', [('reg', 64), ('rm', 8)])
    Add('0f bf', 'movsx', [('reg', 64), ('rm', 16)])
  else:
    Add('0f b6', 'movzx', [('reg', 32), ('rm', 8)])
    Add('0f b6', 'movzx', [('reg', 16), ('rm', 8)], data16=True)
    Add('0f b7', 'movzx', [('reg', 32), ('rm', 16)])
    Add('0f be', 'movsx', [('reg', 32), ('rm', 8)])
    Add('0f be', 'movsx', [('reg', 16), ('rm', 8)], data16=True)
    Add('0f bf', 'movsx', [('reg', 32), ('rm', 16)])

  # x86-64 only.  For x86-32, this opcode is used by 'arpl'.
  # TODO: The original x86-64 validator accepts this without a REX.W
  # prefix, although that is not very useful because then this
  # zero-extends, which is the same as 'mov'.
  if rex_w:
    Add('63', 'movsxd', [('reg', 64), ('rm', 32)])

  AddLW2('f3 0f b8', 'popcnt', ['reg', 'rm'])
  AddLW2('f3 0f bd', 'lzcnt', ['reg', 'rm'])

  # Added in the 486.
  AddPair2('0f', 0xb0, 'cmpxchg', ['rm', 'reg'])
  AddPair2('0f', 0xc0, 'xadd', ['rm', 'reg'])
  # # Group 9 just contains cmpxchg.
  if rex_w:
    Add('0f c7', 'cmpxchg16b', [('mem', 128)], modrm_opcode=1)
  else:
    Add('0f c7', 'cmpxchg8b', [('mem', 64)], modrm_opcode=1)
  for reg_num in range(8):
    # bswap is undefined when used with the data16 prefix (because
    # xchgw could be used for swapping bytes in a word instead),
    # although objdump decodes such instructions.
    Add('0f ' + Byte(0xc8 + reg_num), 'bswap', [(FixReg(reg_num), RexSize(32))])

  AddForm('0f c2', 'cmpps', 'Vps Wps Ib')
  AddForm('f3 0f c2', 'cmpss', 'Vss Wss Ib')
  AddForm('66 0f c2', 'cmppd', 'Vpd Wpd Ib')
  AddForm('f2 0f c2', 'cmpsd', 'Vsd Wsd Ib')
  # binutils incorrectly disassembles 'movnti' with 'QWORD PTR', even
  # though the assembler only accepts 'DWORD PTR'.
  Add('0f c3', 'FIXME movnti', [('mem', 32), ('reg', 32)])
  # # Even though pinsrw only uses the bottom 16 bits of the source
  # # register, it is written as using a 32-bit register.  The 2nd arg
  # # is 'mem16/reg32'.
  # Add('0f c4', 'pinsrw', [('reg', 'mmx'), ('mem', 16), ('imm', 8)])
  # Add('0f c4', 'pinsrw', [('reg', 'mmx'), ('reg2', 32), ('imm', 8)])
  # Add('66 0f c4', 'pinsrw', [('reg', 'xmm'), ('mem', 16), ('imm', 8)])
  # Add('66 0f c4', 'pinsrw', [('reg', 'xmm'), ('reg2', 32), ('imm', 8)])
  # Add('0f c5', 'pextrw', [('reg', 32), ('reg2', 'mmx'), ('imm', 8)])
  # Add('66 0f c5', 'pextrw', [('reg', 32), ('reg2', 'xmm'), ('imm', 8)])
  AddForm('0f c6', 'shufps', 'Vps Wps Ib')
  AddForm('66 0f c6', 'shufpd', 'Vpd Wpd Ib')

  AddForm('66 0f d0', 'addsubpd', 'Vpd Wpd')
  AddForm('f2 0f d0', 'addsubps', 'Vps Wps')
  AddSSEMMXPair('0f d1', 'psrlw')
  AddSSEMMXPair('0f d2', 'psrld')
  AddSSEMMXPair('0f d3', 'psrlq')
  AddSSEMMXPair('0f d4', 'paddq')
  AddSSEMMXPair('0f d5', 'pmullw')
  AddForm('f3 0f d6', 'movq2dq', 'Vdq Nq')
  AddForm('66 0f d6', 'movq', 'Wq Vq')
  AddForm('f2 0f d6', 'movdq2q', 'Pq Uq')
  # AddForm('0f d7', 'pmovmskb', 'Gd Nq')
  # AddForm('66 0f d7', 'pmovmskb', 'Gd Udq')
  AddSSEMMXPair('0f d8', 'psubusb')
  AddSSEMMXPair('0f d9', 'psubusw')
  AddSSEMMXPair('0f da', 'pminub')
  AddSSEMMXPair('0f db', 'pand')
  AddSSEMMXPair('0f dc', 'paddusb')
  AddSSEMMXPair('0f dd', 'paddusw')
  AddSSEMMXPair('0f de', 'pmaxub')
  AddSSEMMXPair('0f df', 'pandn')

  AddSSEMMXPair('0f e0', 'pavgb')
  AddSSEMMXPair('0f e1', 'psraw')
  AddSSEMMXPair('0f e2', 'psrad')
  AddSSEMMXPair('0f e3', 'pavgw')
  AddSSEMMXPair('0f e4', 'pmulhuw')
  AddSSEMMXPair('0f e5', 'pmulhw')
  AddForm('f3 0f e6', 'cvtdq2pd', 'Vpd Wq')
  AddForm('66 0f e6', 'cvttpd2dq', 'Vq Wpd')
  AddForm('f2 0f e6', 'cvtpd2dq', 'Vq Wpd')
  AddForm('0f e7', 'movntq', 'Mq Pq')
  AddForm('66 0f e7', 'movntdq', 'Mdq Vdq')
  AddSSEMMXPair('0f e8', 'psubsb')
  AddSSEMMXPair('0f e9', 'psubsw')
  AddSSEMMXPair('0f ea', 'pminsw')
  AddSSEMMXPair('0f eb', 'por')
  AddSSEMMXPair('0f ec', 'paddsb')
  AddSSEMMXPair('0f ed', 'paddsw')
  AddSSEMMXPair('0f ee', 'pmaxsw')
  AddSSEMMXPair('0f ef', 'pxor')

  # This should be 'Vpd Mdq', but objdump omits the 'XMMWORD' string.
  Add('f2 0f f0', 'lddqu', [('reg', 'xmm'), ('mem', 'lddqu_size')]) 
  AddSSEMMXPair('0f f1', 'psllw')
  AddSSEMMXPair('0f f2', 'pslld')
  AddSSEMMXPair('0f f3', 'psllq')
  AddSSEMMXPair('0f f4', 'pmuludq')
  AddSSEMMXPair('0f f5', 'pmaddwd')
  AddSSEMMXPair('0f f6', 'psadbw')
  # TODO: maskmov* requires a memory access mask.
  # AddForm('0f f7', 'maskmovq', 'Pq Nq')
  # AddForm('66 0f f7', 'maskmovdqu', 'Vdq Udq')
  AddSSEMMXPair('0f f8', 'psubb')
  AddSSEMMXPair('0f f9', 'psubw')
  AddSSEMMXPair('0f fa', 'psubd')
  AddSSEMMXPair('0f fb', 'psubq')
  AddSSEMMXPair('0f fc', 'paddb')
  AddSSEMMXPair('0f fd', 'paddw')
  AddSSEMMXPair('0f fe', 'paddd')

  # SSE
  # Group 15
  if not nacl_mode:
    if rex_w:
      Add('0f ae', 'fxsave64', [('mem', 'fxsave_size')], modrm_opcode=0)
      Add('0f ae', 'fxrstor64', [('mem', 'fxsave_size')], modrm_opcode=1)
    else:
      Add('0f ae', 'fxsave', [('mem', 'fxsave_size')], modrm_opcode=0)
      Add('0f ae', 'fxrstor', [('mem', 'fxsave_size')], modrm_opcode=1)
  Add('0f ae', 'ldmxcsr', [('mem', 32)], modrm_opcode=2)
  Add('0f ae', 'stmxcsr', [('mem', 32)], modrm_opcode=3)
  # TODO: The AMD manual permits 8 different encodings of each of
  # these three instructions (with any value of the 3-bit RM field).
  # The original x86-32 validator allows all of these.  However,
  # objdump refuses to decode all but the RM==0 encodings.
  Add('0f ae e8', 'lfence', []) # modrm_opcode=5
  Add('0f ae f0', 'mfence', []) # modrm_opcode=6
  Add('0f ae f8', 'sfence', []) # modrm_opcode=7
  Add('0f ae', 'clflush', [('mem', 8)], modrm_opcode=7)

  # x87 floating point instructions.

  AddFPRM('d8', 'fadd', modrm_opcode=0)
  AddFPRM('d8', 'fmul', modrm_opcode=1)
  AddFPRM('d8', 'fcom', modrm_opcode=2, format='reg')
  AddFPRM('d8', 'fcomp', modrm_opcode=3, format='reg')
  AddFPRM('d8', 'fsub', modrm_opcode=4)
  AddFPRM('d8', 'fsubr', modrm_opcode=5)
  AddFPRM('d8', 'fdiv', modrm_opcode=6)
  AddFPRM('d8', 'fdivr', modrm_opcode=7)

  AddFPMem('d9', 'fld', modrm_opcode=0)
  # skip 1
  AddFPMem('d9', 'fst', modrm_opcode=2)
  AddFPMem('d9', 'fstp', modrm_opcode=3)
  AddFPMem('d9', 'fldenv', modrm_opcode=4, size='other_x87_size')
  AddFPMem('d9', 'fldcw', modrm_opcode=5, size=16)
  AddFPMem('d9', 'fnstenv', modrm_opcode=6, size='other_x87_size')
  AddFPMem('d9', 'fnstcw', modrm_opcode=7, size=16)

  AddFPReg('d9', 'fld', modrm_opcode=0, format='reg')
  AddFPReg('d9', 'fxch', modrm_opcode=1, format='reg')
  # /2:
  Add('d9 d0', 'fnop', [])
  # /4:
  Add('d9 e0', 'fchs', [])
  Add('d9 e1', 'fabs', [])
  # invalid: e2
  # invalid: e3
  Add('d9 e4', 'ftst', [])
  Add('d9 e5', 'fxam', [])
  # invalid: e6
  # invalid: e7
  # /5:
  Add('d9 e8', 'fld1', [])
  Add('d9 e9', 'fldl2t', [])
  Add('d9 ea', 'fldl2e', [])
  Add('d9 eb', 'fldpi', [])
  Add('d9 ec', 'fldlg2', [])
  Add('d9 ed', 'fldln2', [])
  Add('d9 ee', 'fldz', [])
  # invalid: ef
  # /6:
  Add('d9 f0', 'f2xm1', [])
  Add('d9 f1', 'fyl2x', [])
  Add('d9 f2', 'fptan', [])
  Add('d9 f3', 'fpatan', [])
  Add('d9 f4', 'fxtract', [])
  Add('d9 f5', 'fprem1', [])
  Add('d9 f6', 'fdecstp', [])
  Add('d9 f7', 'fincstp', [])
  # /7:
  Add('d9 f8', 'fprem', [])
  Add('d9 f9', 'fyl2xp1', [])
  Add('d9 fa', 'fsqrt', [])
  Add('d9 fb', 'fsincos', [])
  Add('d9 fc', 'frndint', [])
  Add('d9 fd', 'fscale', [])
  Add('d9 fe', 'fsin', [])
  Add('d9 ff', 'fcos', [])

  AddFPMem('da', 'fiadd', modrm_opcode=0)
  AddFPMem('da', 'fimul', modrm_opcode=1)
  AddFPMem('da', 'ficom', modrm_opcode=2)
  AddFPMem('da', 'ficomp', modrm_opcode=3)
  AddFPMem('da', 'fisub', modrm_opcode=4)
  AddFPMem('da', 'fisubr', modrm_opcode=5)
  AddFPMem('da', 'fidiv', modrm_opcode=6)
  AddFPMem('da', 'fidivr', modrm_opcode=7)

  AddFPReg('da', 'fcmovb', modrm_opcode=0)
  AddFPReg('da', 'fcmove', modrm_opcode=1)
  AddFPReg('da', 'fcmovbe', modrm_opcode=2)
  AddFPReg('da', 'fcmovu', modrm_opcode=3)
  Add('da e9', 'fucompp', [])

  AddFPMem('db', 'fild', modrm_opcode=0)
  AddFPMem('db', 'fisttp', modrm_opcode=1)
  AddFPMem('db', 'fist', modrm_opcode=2)
  AddFPMem('db', 'fistp', modrm_opcode=3)
  # skip 4 and 6
  AddFPMem('db', 'fld', modrm_opcode=5, size=80)
  AddFPMem('db', 'fstp', modrm_opcode=7, size=80)

  AddFPReg('db', 'fcmovnb', modrm_opcode=0)
  AddFPReg('db', 'fcmovne', modrm_opcode=1)
  AddFPReg('db', 'fcmovnbe', modrm_opcode=2)
  AddFPReg('db', 'fcmovnu', modrm_opcode=3)
  # /4:
  Add('db e2', 'fnclex', [])
  Add('db e3', 'fninit', [])
  AddFPReg('db', 'fucomi', modrm_opcode=5)
  AddFPReg('db', 'fcomi', modrm_opcode=6)

  AddFPRM('dc', 'fadd', modrm_opcode=0, size=64, format='reg st')
  AddFPRM('dc', 'fmul', modrm_opcode=1, size=64, format='reg st')
  AddFPMem('dc', 'fcom', modrm_opcode=2, size=64)
  AddFPMem('dc', 'fcomp', modrm_opcode=3, size=64)
  AddFPRM('dc', 'fsub', modrm_opcode=4, size=64, format='reg st')
  AddFPRM('dc', 'fsubr', modrm_opcode=5, size=64, format='reg st')
  AddFPRM('dc', 'fdiv', modrm_opcode=6, size=64, format='reg st')
  AddFPRM('dc', 'fdivr', modrm_opcode=7, size=64, format='reg st')

  AddFPMem('dd', 'fld', modrm_opcode=0, size=64)
  AddFPMem('dd', 'fisttp', modrm_opcode=1, size=64)
  AddFPRM('dd', 'fst', modrm_opcode=2, size=64, format='reg')
  AddFPRM('dd', 'fstp', modrm_opcode=3, size=64, format='reg')
  AddFPMem('dd', 'frstor', modrm_opcode=4, size='other_x87_size')
  # skip 5
  AddFPMem('dd', 'fnsave', modrm_opcode=6, size='other_x87_size')
  AddFPMem('dd', 'fnstsw', modrm_opcode=7, size=16)
  AddFPReg('dd', 'ffree', modrm_opcode=0, format='reg')
  # skip 1, 6, 7
  AddFPReg('dd', 'fucom', modrm_opcode=4, format='reg')
  AddFPReg('dd', 'fucomp', modrm_opcode=5, format='reg')

  AddFPMem('de', 'fiadd', modrm_opcode=0, size=16)
  AddFPMem('de', 'fimul', modrm_opcode=1, size=16)
  AddFPMem('de', 'ficom', modrm_opcode=2, size=16)
  AddFPMem('de', 'ficomp', modrm_opcode=3, size=16)
  AddFPMem('de', 'fisub', modrm_opcode=4, size=16)
  AddFPMem('de', 'fisubr', modrm_opcode=5, size=16)
  AddFPMem('de', 'fidiv', modrm_opcode=6, size=16)
  AddFPMem('de', 'fidivr', modrm_opcode=7, size=16)

  AddFPReg('de', 'faddp', modrm_opcode=0, format='reg st')
  AddFPReg('de', 'fmulp', modrm_opcode=1, format='reg st')
  # skip 2
  Add('de d9', 'fcompp', [])
  AddFPReg('de', 'fsubp', modrm_opcode=4, format='reg st')
  AddFPReg('de', 'fsubrp', modrm_opcode=5, format='reg st')
  AddFPReg('de', 'fdivp', modrm_opcode=6, format='reg st')
  AddFPReg('de', 'fdivrp', modrm_opcode=7, format='reg st')

  AddFPMem('df', 'fild', modrm_opcode=0, size=16)
  AddFPMem('df', 'fisttp', modrm_opcode=1, size=16)
  AddFPMem('df', 'fist', modrm_opcode=2, size=16)
  AddFPMem('df', 'fistp', modrm_opcode=3, size=16)
  AddFPMem('df', 'fbld', modrm_opcode=4, size=80)
  AddFPMem('df', 'fild', modrm_opcode=5, size=64)
  AddFPMem('df', 'fbstp', modrm_opcode=6, size=80)
  AddFPMem('df', 'fistp', modrm_opcode=7, size=64)
  # skip 0-3
  Add('df e0', 'fnstsw', [('*ax', 16)])
  AddFPReg('df', 'fucomip', modrm_opcode=5)
  AddFPReg('df', 'fcomip', modrm_opcode=6)
  # skip 7

  Add3DNow([
      (0x90, 'pfcmpge'),
      (0xa0, 'pfcmpgt'),
      (0xb0, 'pfcmpeq'),
      (0x94, 'pfmin'),
      (0xa4, 'pfmax'),
      (0xb4, 'pfmul'),
      (0x96, 'pfrcp'),
      (0xa6, 'pfrcpit1'),
      (0xb6, 'pfrcpit2'),
      (0x97, 'pfrsqrt'),
      (0xa7, 'pfrsqit1'),
      (0xb7, 'pmulhrw'),
      (0x0c, 'pi2fw'),
      (0x1c, 'pf2iw'),
      (0x0d, 'pi2fd'),
      (0x1d, 'pf2id'),
      (0x8a, 'pfnacc'),
      (0x9a, 'pfsub'),
      (0xaa, 'pfsubr'),
      (0xbb, 'pswapd'),
      (0x8e, 'pfpnacc'),
      (0x9e, 'pfadd'),
      (0xae, 'pfacc'),
      (0xbf, 'pavgusb'),
      ])

  return top_nodes


def GetRoot(nacl_mode):
  Log('Core instructions...')
  core = GetRexRoot(nacl_mode=nacl_mode)
  # Not for x86-64.
  # Log('Memory access instructions...')
  # mem = TrieOfList(['65'], DftLabel('gs_prefix', None,
  #                                   GetRexRoot(nacl_mode=nacl_mode,
  #                                              mem_access_only=True,
  #                                              gs_access_only=True)))
  Log('Locked instructions...')
  lock = TrieOfList(['f0'], DftLabel('lock_prefix', None,
                                     GetRexRoot(nacl_mode=nacl_mode,
                                                mem_access_only=True,
                                                lockable_only=True)))
  Log('Merge...')
  return MergeMany([core, lock], NoMerge)


def ExpandArg((do_expand, arg), label_map):
  if do_expand:
    return label_map['%s_arg' % arg]
  else:
    return arg

def InstrFromLabels(label_map):
  # XXX: Not for x86-64.
  if 'gs_prefix' in label_map:
    # Modifying the string to add 'gs:' is rather hacky, but it is
    # probably not worth doing it more cleanly, because NaCl has been
    # changed so that the %gs segment is only 4 bytes, and the
    # validator will probably be changed to disallow all but the
    # simplest %gs usage.
    if 'rm_arg' in label_map:
      label_map['rm_arg'] = \
          label_map['rm_arg'].replace('ds:', 'gs:').replace('[', 'gs:[')
    elif 'mem_arg' in label_map:
      label_map['mem_arg'] = label_map['mem_arg'].replace('ds:', 'gs:')
    else:
      raise AssertionError('Bad gs prefix usage?')
  instr_args = ','.join([' ' + ExpandArg(arg, label_map)
                         for arg in label_map['args']])
  instr = label_map['instr_name'] + instr_args
  if 'lock_prefix' in label_map:
    instr = 'lock ' + instr
  return instr

def GetAll(node):
  for bytes, label_map in FlattenTrie(node):
    yield (bytes, InstrFromLabels(label_map))


# This is hacky.  To allow a superinstruction to be merged with the
# main trie, it needs to have a 'zeroextends' label in the same place.
def CopyInLabel(bytes, node):
  if len(bytes) == 0:
    return trie.MakeInterned({}, 'normal_inst')
  elif isinstance(node, DftLabel):
    assert node.key == 'zeroextends'
    return DftLabel(node.key, node.value, CopyInLabel(bytes, node.next))
  else:
    child = node.children.get(bytes[0], trie.EmptyNode)
    return TrieOfList([bytes[0]], CopyInLabel(bytes[1:], child))


def SuperInsts():
  for reg in range(8):
    # The original x86-32 validator arbitrarily disallows %esp here,
    # but we allow it.
    mask = [0x83, 0xe0 | reg, 0xe0,  # and $~31, %reg
            0x4c, 0x01, 0xf8 | reg]  # add %r15, %reg
    jmp = [0xff, 0xe0 | reg]  # jmp *%reg
    call = [0xff, 0xd0 | reg]  # call *%reg
    yield map(Byte, mask + jmp)
    yield map(Byte, mask + call)

    # The original x86-64 validator allows useless 0x40 REX prefixes
    # for top-bit-clear registers, but we don't.

    # Top-bit-set registers.
    # Exclude r15 since jumping using that would trash r15 and cause a
    # jump to the bottom 4GB.
    # Jumping using rsp or rbp is allowed but useless.
    if reg != 7:
      mask = [0x41, 0x83, 0xe0 | reg, 0xe0,  # and $~31, %reg
              0x4d, 0x01, 0xf8 | reg]  # add %r15, %reg
      jmp = [0x41, 0xff, 0xe0 | reg]  # jmp *%reg
      call = [0x41, 0xff, 0xd0 | reg]  # call *%reg
      yield map(Byte, mask + jmp)
      yield map(Byte, mask + call)

  # TODO: Also allow non-canonical register orderings.
  yield map(Byte, [0x48, 0x89, 0xe5]) # mov %rsp, %rbp
  yield map(Byte, [0x48, 0x89, 0xec]) # mov %rbp, %rsp

  def Munge(bytes):
    return bytes.split()
  # Long nops
  # TODO: Add decodings of these instructions.
  yield Munge('0f 1f 00')
  yield Munge('0f 1f 40 00')
  yield Munge('0f 1f 44 00 00')
  yield Munge('66 0f 1f 44 00 00')
  yield Munge('0f 1f 80 00 00 00 00')
  yield Munge('0f 1f 84 00 00 00 00 00')
  yield Munge('66 0f 1f 84 00 00 00 00 00')
  yield Munge('66 2e 0f 1f 84 00 00 00 00 00')
  yield Munge('66 66 2e 0f 1f 84 00 00 00 00 00')
  yield Munge('66 66 66 2e 0f 1f 84 00 00 00 00 00')
  yield Munge('66 66 66 66 2e 0f 1f 84 00 00 00 00 00')
  yield Munge('66 66 66 66 66 2e 0f 1f 84 00 00 00 00 00')
  yield Munge('66 66 66 66 66 66 2e 0f 1f 84 00 00 00 00 00')

  # String operations.
  fix_rsi = Munge('89 f6 '        # mov esi, esi
                  '49 8d 34 37')  # lea rsi, [r15+rsi]
  fix_rdi = Munge('89 ff '        # mov edi, edi
                  '49 8d 3c 3f')  # lea rdi, [r15+rdi]
  string_ops = [
      (0xa4, 'movs', fix_rsi + fix_rdi),
      (0xaa, 'stos', fix_rdi),
      # TODO: Check whether 'lods' should really be allowed.
      # (0xac, 'lods', fix_rsi),
      (0xa6, 'cmps', fix_rsi + fix_rdi),
      (0xae, 'scas', fix_rdi),
      ]
  for opcode, instr_name, fixes in string_ops:
    for prefix_bytes, prefix in [([], ''),
                                 (['f2'], 'repnz '),
                                 (['f3'], 'rep ')]:
      # repnz is not allowed with movs/stos, though that may just be a
      # mistake in the original validator.  TODO: Check this.
      if prefix + instr_name in ('repnz movs', 'repnz stos'):
        continue
      yield fixes + prefix_bytes + [Byte(opcode)] # 8-bit
      # Combining the data16 prefix with rep/repnz is not allowed.
      if prefix == '':
        yield fixes + ['66'] + prefix_bytes + [Byte(opcode + 1)] # 16-bit
      yield fixes + prefix_bytes + [Byte(opcode + 1)] # 32-bit
      yield fixes + prefix_bytes + ['48', Byte(opcode + 1)] # 64-bit


def MergeAcceptTypes(accept_types):
  if accept_types == set(['normal_inst', False]):
    return 'superinst_start'
  else:
    raise AssertionError('Cannot merge %r' % accept_types)


def FilterPrefixRex(prefix, trie):
  nodes = [FilterPrefix(prefix, trie)]
  for rex_bits in range(0x10):
    nodes.append(FilterPrefix([Byte(0x40 | rex_bits)] + prefix, trie))
  return MergeMany(nodes, NoMerge)


def WriteInstructionList(filename, trie):
  fh = open(filename, 'w')
  for bytes, labels in FlattenTrie(trie):
    suffix = ''
    for key in ('requires_fixup', 'requires_zeroextend', 'zeroextends'):
      if key in labels:
        suffix += ' {%s:%s}' % (key, labels[key])
    fh.write('%s:%s%s\n' % (' '.join(bytes), InstrFromLabels(labels), suffix))
  fh.close()


def Main():
  # Limit memory usage to prevent mistakes from trashing the system.
  limit = 1000 << 20
  resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

  Log('Building trie...')
  trie_root = GetRoot(nacl_mode=True)
  Log('Size:')
  Log(TrieSize(trie_root, False))
  Log('Node count:')
  Log(TrieNodeCount(trie_root))
  Log('Building test subset...')
  filtered_trie = FilterModRM(trie_root)
  Log('Testing...')
  WriteInstructionList('examples.list', filtered_trie)
  bits = 64
  objdump_check.DisassembleTest(lambda: GetAll(filtered_trie), bits=bits)

  Log('Testing all ModRM bytes...')
  modrm_trie = FilterPrefixRex(['01'], trie_root)
  WriteInstructionList('examples-modrm.list', modrm_trie)
  objdump_check.DisassembleTest(lambda: GetAll(modrm_trie), bits=bits)
  Log('Testing all ModRM bytes with gs...')
  objdump_check.DisassembleTest(
      lambda: GetAll(FilterPrefix(['65', '89'], trie_root)),
      bits=bits)

  Log('Converting to DFA...')
  dfa_root = StripDft(trie_root)
  Log('DFA node count:')
  Log(TrieNodeCount(dfa_root))
  Log('Expand wildcards...')
  # This is much faster as a separate pass that is applied after
  # StripDft(), because there are fewer nodes to apply the
  # expanding-out to.
  dfa_root = ExpandWildcards(dfa_root)
  Log('DFA node count:')
  Log(TrieNodeCount(dfa_root))

  Log('Adding jumps...')
  superinsts = [CopyInLabel(bytes, dfa_root) for bytes in SuperInsts()]
  dfa_root = MergeMany([dfa_root] + superinsts, MergeAcceptTypes)
  Log('DFA node count:')
  Log(TrieNodeCount(dfa_root))
  dest_file = 'x86_64.trie'
  Log('Dumping trie to %r...' % dest_file)
  trie.WriteToFile(dest_file, dfa_root)
  Log('Done')


if __name__ == '__main__':
  Main()
