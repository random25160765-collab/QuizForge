/* ===========================================================================
 * highlight.js —— 手写语法高亮器
 *
 * 覆盖 C / C++ / CUDA / PTX / Rust / Python / Bash / ASM / JSON / YAML / TOML。
 * 本机无外网，无法引入 highlight.js 或 shiki，因此自己实现：
 * 单遍字符扫描，识别 注释 → 字符串 → 预处理指令 → 数字 → 寄存器 → 标识符 → 标点。
 *
 * 输出 <span class="tok-*">，样式在 theme/markdown.css 里定义。
 * ========================================================================= */
(function () {
  'use strict';

  var QF = (window.QF = window.QF || {});

  function esc(text) {
    return String(text).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function set(list) {
    var out = {};
    String(list)
      .split(/\s+/)
      .filter(Boolean)
      .forEach(function (w) {
        out[w] = true;
      });
    return out;
  }

  /* ------------------------------------------------------------- C 家族 */

  var C_KEYWORDS = set(
    'auto break case char const continue default do double else enum extern float for goto if inline int long ' +
      'register restrict return short signed sizeof static struct switch typedef union unsigned void volatile while ' +
      '_Bool _Complex _Atomic _Alignas _Alignof _Generic _Noreturn _Static_assert _Thread_local ' +
      'asm typeof'
  );
  var C_TYPES = set(
    'size_t ssize_t ptrdiff_t intptr_t uintptr_t FILE va_list bool ' +
      'int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t ' +
      'int_least8_t int_fast8_t off_t time_t pid_t'
  );
  var C_BUILTINS = set(
    'printf fprintf sprintf snprintf puts putchar scanf sscanf malloc calloc realloc free memcpy memmove memset ' +
      'memcmp strlen strcpy strncpy strcmp strcat strchr strstr strtol strtoul atoi exit abort assert ' +
      'open close read write lseek mmap munmap fork execve waitpid pthread_create pthread_join'
  );
  var C_CONSTANTS = set('NULL true false EOF');

  var CPP_KEYWORDS = set(
    'alignas alignof and and_eq asm auto bitand bitor break case catch char char8_t char16_t char32_t class compl ' +
      'concept const consteval constexpr constinit const_cast continue co_await co_return co_yield decltype default ' +
      'delete do double dynamic_cast else enum explicit export extern false final float for friend goto if inline ' +
      'int long mutable namespace new noexcept not not_eq nullptr operator or or_eq override private protected public ' +
      'reflexpr register reinterpret_cast requires return short signed sizeof static static_assert static_cast struct ' +
      'switch template this thread_local throw true try typedef typeid typename union unsigned using virtual void ' +
      'volatile wchar_t while xor xor_eq'
  );
  var CPP_TYPES = set(
    'bool size_t ptrdiff_t int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t ' +
    'string wstring string_view vector array deque list forward_list map multimap unordered_map set multiset ' +
    'unordered_set stack queue priority_queue pair tuple optional variant any function bind less greater ' +
    'unique_ptr shared_ptr weak_ptr enable_shared_from_this iterator const_iterator allocator ' +
    'istream ostream iostream ifstream ofstream stringstream thread mutex condition_variable atomic chrono filesystem ' +
    'int64_t uint64_t uintptr_t'
  );
  var CPP_BUILTINS = set(
    'std cout cin cerr clog endl flush move forward swap make_pair make_tuple make_unique make_shared ' +
    'begin end rbegin rend size data empty push_back emplace_back pop_back push_front pop_front insert erase ' +
    'find count sort stable_sort nth_element partial_sort lower_bound upper_bound equal_range reverse unique ' +
    'fill fill_n copy copy_n transform accumulate inner_product iota for_each all_of any_of none_of remove_if ' +
    'min max min_element max_element clamp exchange as_const mem_fn ref cref invoke apply visit get holds_alternative ' +
    'static_pointer_cast dynamic_pointer_cast const_pointer_cast alignof sizeof typeid printf memcpy memset'
  );

  var CUDA_KEYWORDS = set(
    '__global__ __device__ __host__ __forceinline__ __inline__ __noinline__ __restrict__ __launch_bounds__ ' +
      '__shared__ __constant__ __managed__ __grid_constant__ __align__ __device_builtin__ __cudart_builtin__'
  );
  var CUDA_BUILTINS = set(
    'blockIdx threadIdx blockDim gridDim warpSize laneid nsmid ' +
      '__syncthreads __syncwarp __threadfence __threadfence_block __threadfence_system __ballot_sync __any_sync ' +
      '__all_sync __shfl_sync __shfl_up_sync __shfl_down_sync __shfl_xor_sync __activemask __popc __clz __ffs ' +
      '__fdividef __fmaf_rn __fmul_rn __fadd_rn __expf __logf __powf __saturatef __float2half __half2float ' +
      'atomicAdd atomicSub atomicExch atomicMin atomicMax atomicInc atomicDec atomicCAS atomicAnd atomicOr atomicXor ' +
      'atomicAdd_block atomicAdd_system ' +
      'cudaMalloc cudaFree cudaMemcpy cudaMemcpyAsync cudaMemset cudaDeviceSynchronize cudaGetLastError ' +
      'cudaGetErrorString cudaStreamCreate cudaStreamDestroy cudaStreamSynchronize cudaEventCreate cudaEventRecord ' +
      'cudaEventElapsedTime cudaMallocManaged cudaHostAlloc cudaSetDevice cudaGetDeviceProperties cudaOccupancyMaxActiveBlocksPerMultiprocessor'
  );
  var CUDA_TYPES = set(
    'dim3 cudaError_t cudaStream_t cudaEvent_t cudaStream_t cudaDeviceProp float2 float3 float4 double2 ' +
      'half __half __half2 __nv_bfloat16 nv_bfloat16 cublasHandle_t cublasOperation_t ' +
      'cooperative_groups cg this_thread'
  );

  /* ------------------------------------------------------------------ PTX */

  var PTX_KEYWORDS = set(
    '.version .target .address_size .file .loc .pragma .section .visible .weak .extern .common ' +
      '.entry .func .global .shared .local .const .param .reg .sreg .pred .tex .surf ' +
      '.align .type .size .space .b8 .b16 .b32 .b64 .b128 .u8 .u16 .u32 .u64 .s8 .s16 .s32 .s64 ' +
      '.f16 .f32 .f64 .v2 .v4 .v8 .ptr .uptr .sptr .global .shared .local .const .param .generic ' +
      '.rn .rz .rm .rp .ftz .sat .hi .lo .wide .sm .cg .cs .lu .cv .wb .ca .wt .eq .ne .lt .le .gt .ge .lo .ls .hs .eq '
  );
  var PTX_INSTRUCTIONS = set(
    'abs add addc and atom bar barrier bfe bfi bfind bmsk bra brev brkpt call clz cnot copysign cos cp cvt ' +
      'discard div dp2a dp4a ex2 exit fma fns lg2 mad mad24 max membar min mov mul neg not or pmevent popc ' +
      'prefetch ptx rcp red rem ret rsqrt sad selp set setp shf shl shr sin slct sqrt st sub suld suq sur sured ' +
      'sust suxc testp tex tlbie trap vabs vadd vavrg vcmp vdiv vmad vmax vmin vmnmx vmul vneg vsub vsad vset ' +
      'vshl vshr vsqrt vote wmma'
  );
  var PTX_TYPES = set(
    'ld ldv ldu stv st red atom wmma mma ldmatrix stmatrix cp.async createpolicy ' +
      'tid ntid ctaid nctaid laneid warpid nwarpid smid nsmid gridid clock clock64 lanemask_eq lanemask_lt ' +
      'lanemask_le lanemask_gt lanemask_ge pm0 pm1 pm2 pm3 envreg0 envreg1 globaltimer total_smem_size ' +
      'dynamic_smem_size maxntid reqntid minnctapersm maxnctapersm'
  );

  /* ----------------------------------------------------------------- Rust */

  var RUST_KEYWORDS = set(
    'as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move ' +
      'mut pub ref return self Self static struct super trait true type unsafe use where while yield ' +
      'macro_rules union default'
  );
  var RUST_TYPES = set(
    'i8 i16 i32 i64 i128 isize u8 u16 u32 u64 u128 usize f32 f64 bool char str ' +
      'String Vec Option Result Box Rc Arc RefCell Cell Cow HashMap HashSet BTreeMap BTreeSet VecDeque ' +
      'PathBuf Path Iter Iterator IntoIterator Some None Ok Err Ordering Duration Instant ' +
      'Send Sync Sized Copy Clone Debug Display Default PartialEq Eq Hash Drop Deref AsRef Into From Iterator'
  );
  var RUST_BUILTINS = set(
    'println print format eprint eprintln vec panic assert assert_eq assert_ne debug_assert todo unimplemented ' +
      'unreachable write writeln matches dbg include_str include_bytes env concat stringify line column file module_path'
  );
  var RUST_ATTRS = set(
    'derive cfg test allow warn deny doc inline cold must_use deprecated no_mangle repr serde clippy'
  );

  /* --------------------------------------------------------------- Python */

  var PY_KEYWORDS = set(
    'and as assert async await break class continue def del elif else except finally for from global if import in is ' +
      'lambda nonlocal not or pass raise return try while with yield match case'
  );
  var PY_BUILTINS = set(
    'abs aiter all anext any ascii bin bool breakpoint bytearray bytes callable chr classmethod compile complex ' +
      'delattr dict dir divmod enumerate eval exec filter float format frozenset getattr globals hasattr hash help ' +
      'hex id input int isinstance issubclass iter len list locals map max memoryview min next object oct open ord ' +
      'pow print property range repr reversed round set setattr slice sorted staticmethod str sum super tuple type ' +
      'vars zip __import__ self cls None True False NotImplemented Ellipsis'
  );
  var PY_TYPES = set(
    'int float str bytes bool list dict set tuple frozenset complex bytearray memoryview object type ' +
      'Optional List Dict Set Tuple Any Callable Iterable Sequence Union'
  );

  var BASH_KEYWORDS = set(
    'if then else elif fi for while until do done case esac function in return break continue local export declare ' +
      'readonly unset shift source echo printf read cd exit set trap eval exec'
  );
  var BASH_BUILTINS = set('grep sed awk cat ls mkdir rm cp mv find xargs sort uniq head tail wc cut tr curl git make');

  var ASM_KEYWORDS = set(
    'mov lea add sub mul imul div idiv and or xor not neg shl shr sar sal inc dec cmp test jmp je jne jz jnz jg jl ' +
      'jge jle ja jb call ret push pop nop syscall leave enter int iret ' +
      'ldr str ldp stp b bl cbz cbnz adr adrp ret movz movk'
  );

  /* ---------------------------------------------------------- 语言配置表 */

  function makeSpec(config) {
    return {
      lineComment: config.lineComment || [],
      blockComment: config.blockComment || null,
      strings: config.strings || [{ quote: '"' }, { quote: "'" }],
      keywords: config.keywords || {},
      types: config.types || {},
      builtins: config.builtins || {},
      constants: config.constants || set('true false null NULL None True False'),
      decorators: !!config.decorators,
      preproc: !!config.preproc,
      registers: !!config.registers,
      attributes: config.attributes || null,
      directive: !!config.directive,
    };
  }

  var C_SPEC = makeSpec({
    lineComment: ['//'],
    blockComment: ['/*', '*/'],
    keywords: C_KEYWORDS,
    types: C_TYPES,
    builtins: C_BUILTINS,
    constants: C_CONSTANTS,
    preproc: true,
  });

  var CPP_SPEC = makeSpec({
    lineComment: ['//'],
    blockComment: ['/*', '*/'],
    keywords: CPP_KEYWORDS,
    types: CPP_TYPES,
    builtins: CPP_BUILTINS,
    constants: C_CONSTANTS,
    preproc: true,
  });

  var CUDA_SPEC = makeSpec({
    lineComment: ['//'],
    blockComment: ['/*', '*/'],
    keywords: CPP_KEYWORDS,
    types: CPP_TYPES,
    builtins: CPP_BUILTINS,
    constants: C_CONSTANTS,
    preproc: true,
  });
  ['__global__', '__device__', '__host__', '__shared__', '__constant__', '__restrict__', '__forceinline__',
    '__launch_bounds__', '__syncthreads', '__syncwarp', '__threadfence', '__ballot_sync', '__shfl_sync',
    '__activemask', '__syncthreads_count', '__syncthreads_and', '__syncthreads_or', '__ldg', '__stcg']
    .forEach(function (w) {
      CUDA_SPEC.keywords[w] = true;
    });
  Object.keys(CUDA_BUILTINS).forEach(function (w) {
    CUDA_SPEC.builtins[w] = true;
  });
  Object.keys(CUDA_TYPES).forEach(function (w) {
    CUDA_SPEC.types[w] = true;
  });

  var PTX_SPEC = makeSpec({
    lineComment: ['//'],
    blockComment: ['/*', '*/'],
    keywords: PTX_KEYWORDS,
    types: PTX_TYPES,
    builtins: PTX_INSTRUCTIONS,
    constants: set('true false'),
    registers: true,
    directive: true,
  });

  var RUST_SPEC = makeSpec({
    lineComment: ['//'],
    blockComment: ['/*', '*/'],
    keywords: RUST_KEYWORDS,
    types: RUST_TYPES,
    builtins: RUST_BUILTINS,
    constants: set('true false None Some Ok Err'),
    attributes: RUST_ATTRS,
  });
  RUST_SPEC.lifetimes = true;
  RUST_SPEC.rawStrings = true;

  var PY_SPEC = makeSpec({
    lineComment: ['#'],
    strings: [{ quote: '"' }, { quote: "'" }],
    keywords: PY_KEYWORDS,
    types: PY_TYPES,
    builtins: PY_BUILTINS,
    constants: set('None True False NotImplemented Ellipsis self cls __name__ __main__'),
    decorators: true,
  });
  PY_SPEC.rawStrings = true; // r"..." / f"..." / b"..." 前缀

  var BASH_SPEC = makeSpec({
    lineComment: ['#'],
    keywords: BASH_KEYWORDS,
    builtins: BASH_BUILTINS,
    constants: set('$? $$ $! $# $@ $* $0'),
    variables: true,
  });

  var ASM_SPEC = makeSpec({
    lineComment: [';', '#', '//'],
    keywords: ASM_KEYWORDS,
    registers: true,
    immediate: true,
  });

  var JSON_SPEC = makeSpec({
    lineComment: ['//'],
    keywords: set(''),
    constants: set('true false null'),
    json: true,
  });

  var YAML_SPEC = makeSpec({
    lineComment: ['#'],
    keywords: set(''),
    constants: set('true false null yes no on off'),
    yamlKey: true,
  });

  var TOML_SPEC = makeSpec({
    lineComment: ['#'],
    keywords: set(''),
    constants: set('true false'),
    yamlKey: true,
  });

  var PLAIN_SPEC = makeSpec({ lineComment: [], strings: [] });

  var LANGUAGES = {
    c: C_SPEC,
    h: C_SPEC,
    cpp: CPP_SPEC,
    'c++': CPP_SPEC,
    cc: CPP_SPEC,
    cxx: CPP_SPEC,
    hpp: CPP_SPEC,
    cuda: CUDA_SPEC,
    cu: CUDA_SPEC,
    cuh: CUDA_SPEC,
    ptx: PTX_SPEC,
    sass: PTX_SPEC,
    'ptx-output': PTX_SPEC,
    rust: RUST_SPEC,
    rs: RUST_SPEC,
    python: PY_SPEC,
    py: PY_SPEC,
    bash: BASH_SPEC,
    sh: BASH_SPEC,
    shell: BASH_SPEC,
    zsh: BASH_SPEC,
    asm: ASM_SPEC,
    s: ASM_SPEC,
    json: JSON_SPEC,
    yaml: YAML_SPEC,
    yml: YAML_SPEC,
    toml: TOML_SPEC,
    text: PLAIN_SPEC,
    plain: PLAIN_SPEC,
    txt: PLAIN_SPEC,
    output: PLAIN_SPEC,
    console: PLAIN_SPEC,
    makefile: BASH_SPEC,
    cmake: BASH_SPEC,
    make: BASH_SPEC,
  };

  /* ------------------------------------------------------------ 扫描器 */

  var IDENT_START = /[A-Za-z_$]/;
  var IDENT_CHAR = /[A-Za-z0-9_$]/;
  var DIGIT = /[0-9]/;
  var WS = /\s/;

  function isIdentStart(ch) {
    return ch != null && IDENT_START.test(ch);
  }
  function isIdentChar(ch) {
    return ch != null && IDENT_CHAR.test(ch);
  }

  function classify(word, spec) {
    if (!word) return '';
    if (spec.keywords[word]) return 'kw';
    if (spec.types[word]) return 'type';
    if (spec.builtins[word]) return 'builtin';
    if (spec.constants[word]) return 'const';
    if (spec.attributes && spec.attributes[word]) return 'attr';
    return '';
  }

  function tokenize(src, spec) {
    var out = [];
    var i = 0;
    var n = src.length;
    var buf = '';

    function flushBuf() {
      if (buf) {
        out.push(esc(buf));
        buf = '';
      }
    }
    function emit(cls, text) {
      flushBuf();
      if (cls) out.push('<span class="tok-' + cls + '">' + esc(text) + '</span>');
      else out.push(esc(text));
    }

    /** 从 pos 开始扫描字符串，返回结束位置（不含），支持转义 */
    function scanString(pos, quote, allowEscape) {
      var j = pos + 1;
      while (j < n) {
        var c = src[j];
        if (allowEscape && c === '\\') {
          j += 2;
          continue;
        }
        if (c === quote) return j + 1;
        if (c === '\n') return j; // 未闭合，停在行尾
        j++;
      }
      return n;
    }

    while (i < n) {
      var ch = src[i];

      // 行注释
      var matchedLine = null;
      for (var lc = 0; lc < spec.lineComment.length; lc++) {
        var marker = spec.lineComment[lc];
        if (src.startsWith(marker, i)) {
          matchedLine = marker;
          break;
        }
      }
      if (matchedLine) {
        // #!/... 与 #include 这类交给预处理/普通处理
        var lineEnd = src.indexOf('\n', i);
        if (lineEnd === -1) lineEnd = n;
        emit('com', src.slice(i, lineEnd));
        i = lineEnd;
        continue;
      }

      // 块注释
      if (spec.blockComment && src.startsWith(spec.blockComment[0], i)) {
        var closeAt = src.indexOf(spec.blockComment[1], i + spec.blockComment[0].length);
        var stop = closeAt === -1 ? n : closeAt + spec.blockComment[1].length;
        emit('com', src.slice(i, stop));
        i = stop;
        continue;
      }

      // 预处理指令（C/C++）：整行作为一条
      if (spec.preproc && ch === '#') {
        var eol = i;
        while (eol < n) {
          var nl = src.indexOf('\n', eol);
          if (nl === -1) {
            eol = n;
            break;
          }
          if (src[nl - 1] === '\\') {
            eol = nl + 1;
            continue;
          }
          eol = nl;
          break;
        }
        emit('pre', src.slice(i, eol));
        i = eol;
        continue;
      }

      // Python 装饰器
      if (spec.decorators && ch === '@' && i + 1 < n && isIdentStart(src[i + 1])) {
        var dj = i + 1;
        while (dj < n && (isIdentChar(src[dj]) || src[dj] === '.')) dj++;
        emit('deco', src.slice(i, dj));
        i = dj;
        continue;
      }

      // Rust 属性 #[...] / #![...]
      if (spec.attributes && ch === '#' && src[i + 1] === '[') {
        var depth = 0;
        var aj = i;
        while (aj < n) {
          if (src[aj] === '[') depth++;
          else if (src[aj] === ']') {
            depth--;
            if (!depth) {
              aj++;
              break;
            }
          }
          aj++;
        }
        emit('attr', src.slice(i, aj));
        i = aj;
        continue;
      }

      // Rust 生命周期 / 字符字面量：'a
      if (spec.lifetimes && ch === "'") {
        if (i + 1 < n && /[a-zA-Z_]/.test(src[i + 1]) && src[i + 2] !== "'") {
          var lj = i + 1;
          while (lj < n && isIdentChar(src[lj])) lj++;
          emit('type', src.slice(i, lj));
          i = lj;
          continue;
        }
      }

      // 字符串（含 r"..."、f"..."、b"..."、r#"..."# 前缀）
      if (spec.rawStrings && ch !== '"' && ch !== "'") {
        var pm = /^[rbfu]{1,2}(#*)['"]/.exec(src.slice(i, i + 6));
        if (pm) {
          var quote = src[i + pm[0].length - 1];
          var hashes = pm[1].length;
          var terminator = hashes ? quote + '#'.repeat(hashes) : quote;
          var endIdx = src.indexOf(terminator, i + pm[0].length);
          var end = endIdx === -1 ? n : endIdx + terminator.length;
          emit('str', src.slice(i, end));
          i = end;
          continue;
        }
      }
      if (ch === '"' || ch === "'" || ch === '`') {
        var stopAt = scanString(i, ch, ch !== '`');
        var literal = src.slice(i, stopAt);
        // JSON / YAML 的 "key": 形式单独上色
        if ((spec.json || spec.yamlKey) && ch === '"' && /^\s*:/.test(src.slice(stopAt))) {
          emit('key', literal);
        } else {
          emit('str', literal);
        }
        i = stopAt;
        continue;
      }

      // PTX 寄存器 %r1 / %tid.x
      if (spec.registers && ch === '%') {
        var rj = i + 1;
        while (rj < n && /[A-Za-z0-9_.]/.test(src[rj])) rj++;
        if (rj > i + 1) {
          emit('reg', src.slice(i, rj));
          i = rj;
          continue;
        }
      }

      // 汇编立即数 $0x10 / #123
      if (spec.immediate && (ch === '$' || ch === '#') && DIGIT.test(src[i + 1] || '')) {
        var ij = i + 1;
        while (ij < n && /[0-9a-fA-FxX]/.test(src[ij])) ij++;
        emit('num', src.slice(i, ij));
        i = ij;
        continue;
      }

      // Bash 变量
      if (spec.variables && ch === '$') {
        var vj = i + 1;
        if (src[vj] === '{') {
          var cb = src.indexOf('}', vj);
          vj = cb === -1 ? n : cb + 1;
        } else {
          while (vj < n && /[A-Za-z0-9_@*#?!$]/.test(src[vj])) vj++;
        }
        if (vj > i + 1) {
          emit('var', src.slice(i, vj));
          i = vj;
          continue;
        }
      }

      // 数字
      if (DIGIT.test(ch) && !isIdentChar(src[i - 1])) {
        var nj = i;
        if (src[nj] === '0' && /[xXbBoO]/.test(src[nj + 1] || '')) {
          nj += 2;
          while (nj < n && /[0-9a-fA-F_]/.test(src[nj])) nj++;
        } else {
          while (nj < n && /[0-9_]/.test(src[nj])) nj++;
          if (src[nj] === '.') {
            nj++;
            while (nj < n && /[0-9_]/.test(src[nj])) nj++;
          }
          if (/[eEpP]/.test(src[nj] || '')) {
            nj++;
            if (src[nj] === '+' || src[nj] === '-') nj++;
            while (nj < n && DIGIT.test(src[nj])) nj++;
          }
        }
        // 数值类型后缀：u32 / f16 / ull / _t / h
        var suffix = /^(?:[uU][lL]{0,2}|[lL]{1,2}[uU]?|f32|f64|f16|bf16|h|_t)/.exec(src.slice(nj));
        if (suffix) nj += suffix[0].length;
        emit('num', src.slice(i, nj));
        i = nj;
        continue;
      }

      // JSON / YAML / TOML 的裸键名（引号键名已在字符串分支里处理）
      if (spec.json || spec.yamlKey) {
        if (/[A-Za-z_$]/.test(ch)) {
          var kj = i;
          while (kj < n && /[\w.$-]/.test(src[kj])) kj++;
          var candidate = src.slice(i, kj);
          if (/^\s*[:=]/.test(src.slice(kj))) {
            emit('key', candidate);
            i = kj;
            continue;
          }
        }
      }

      // 标识符 / 关键字
      if (isIdentStart(ch)) {
        var wj = i;
        while (wj < n && isIdentChar(src[wj])) wj++;
        var word = src.slice(i, wj);
        emit(classify(word, spec), word);
        i = wj;
        continue;
      }

      // 其他：标点与空白，累积输出
      buf += ch;
      i++;
    }

    flushBuf();
    return out.join('');
  }

  /* --------------------------------------------------------------- API */

  var cache = {};

  function normalizeLang(lang) {
    var key = String(lang || 'text').toLowerCase().trim();
    if (key === 'c++') key = 'cpp';
    return key;
  }

  /**
   * 优先用 highlight.js（真语法），没有就用手写那份。
   *
   * 手写这份是当年本机无外网时的产物：token 切得粗，遇到没见过的语言常常整段
   * 一个颜色。hljs 到位之后就不必委屈自己了 —— 但**留着它兜底**：
   * 资源没同步（`make vendor`）或加载失败时，代码块仍然看得出结构，
   * 而不是退化成一片没有层次的纯文本。
   */
  function code(source, lang) {
    var text = String(source == null ? '' : source).replace(/\r\n?/g, '\n').replace(/\s+$/, '');
    var key = normalizeLang(lang);
    var viaHljs = useHljs(text, key);
    if (viaHljs !== null) return viaHljs;
    return fallbackCode(text, key);
  }

  function useHljs(text, key) {
    var lib = window.hljs;
    if (!lib || typeof lib.highlight !== 'function') return null;
    try {
      if (key && key !== 'text' && lib.getLanguage && lib.getLanguage(key)) {
        return lib.highlight(text, { language: key, ignoreIllegals: true }).value;
      }
      // 没标语言（或标了个它不认识的）就让它自己猜 —— 猜偏了只是配色差一点，
      // 比整段一个颜色强
      return lib.highlightAuto(text).value;
    } catch (err) {
      return null;
    }
  }

  function fallbackCode(text, key) {
    // 简单的多色高亮开销很小；仅当没有可识别语言时走纯转义
    var cached = cache[key + '\u0001' + text];
    if (cached !== undefined) return cached;
    var spec = LANGUAGES[key];
    var html;
    if (!spec || spec === PLAIN_SPEC) {
      html = esc(text);
    } else {
      try {
        html = tokenize(text, spec);
      } catch (err) {
        html = esc(text);
      }
    }
    if (Object.keys(cache).length > 4000) cache = {};
    cache[key + '\u0001' + text] = html;
    return html;
  }

  function languages() {
    return Object.keys(LANGUAGES).sort();
  }

  QF.highlight = { code: code, languages: languages, tokenize: tokenize };
})();
