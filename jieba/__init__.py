from __future__ import absolute_import, unicode_literals
__version__ = '0.39'
__license__ = 'MIT'

import re
import os
import sys
import time
import logging
import marshal
import tempfile
import threading
from math import log
from hashlib import md5
from ._compat import *
from . import finalseg

if os.name == 'nt':
    from shutil import move as _replace_file
else:
    _replace_file = os.rename

_get_abs_path = lambda path: os.path.normpath(os.path.join(os.getcwd(), path))

DEFAULT_DICT = None
DEFAULT_DICT_NAME = "dict.txt"

log_console = logging.StreamHandler(sys.stderr)
default_logger = logging.getLogger(__name__)
default_logger.setLevel(logging.DEBUG)
default_logger.addHandler(log_console)

DICT_WRITING = {}

pool = None

re_userdict = re.compile('^(.+?)( [0-9]+)?( [a-z]+)?$', re.U)

re_eng = re.compile('[a-zA-Z0-9]', re.U)

# \u4E00-\u9FD5a-zA-Z0-9+#&\._ : All non-space characters. Will be handled with re_han
# \r\n|\s : whitespace characters. Will not be handled.
# re_han_default = re.compile("([\u4E00-\u9FD5a-zA-Z0-9+#&\._%]+)", re.U)
# Adding "-" symbol in re_han_default
# 中文编码&英文&部分符号
re_han_default = re.compile("([\u4E00-\u9FD5a-zA-Z0-9+#&\._%\-]+)", re.U)

re_skip_default = re.compile("(\r\n|\s)", re.U)
re_han_cut_all = re.compile("([\u4E00-\u9FD5]+)", re.U)
re_skip_cut_all = re.compile("[^a-zA-Z0-9+#\n]", re.U)

def setLogLevel(log_level):
    global logger
    default_logger.setLevel(log_level)

class Tokenizer(object):

    def __init__(self, dictionary=DEFAULT_DICT):
        self.lock = threading.RLock()
        if dictionary == DEFAULT_DICT:
            self.dictionary = dictionary
        else:
            self.dictionary = _get_abs_path(dictionary)
        self.FREQ = {}
        self.total = 0
        self.user_word_tag_tab = {}
        self.initialized = False
        self.tmp_dir = None
        self.cache_file = None

    def __repr__(self):
        return '<Tokenizer dictionary=%r>' % self.dictionary

    def gen_pfdict(self, f):
        lfreq = {}
        ltotal = 0
        f_name = resolve_filename(f)
        for lineno, line in enumerate(f, 1):
            try:
                line = line.strip().decode('utf-8')
                word, freq = line.split(' ')[:2]
                freq = int(freq)
                lfreq[word] = freq
                ltotal += freq
                for ch in xrange(len(word)):
                    wfrag = word[:ch + 1]
                    if wfrag not in lfreq:
                        lfreq[wfrag] = 0
            except ValueError:
                raise ValueError(
                    'invalid dictionary entry in %s at Line %s: %s' % (f_name, lineno, line))
        f.close()
        return lfreq, ltotal

    def initialize(self, dictionary=None):
        if dictionary:
            abs_path = _get_abs_path(dictionary)
            if self.dictionary == abs_path and self.initialized:
                return
            else:
                self.dictionary = abs_path
                self.initialized = False
        else:
            abs_path = self.dictionary

        with self.lock:
            try:
                with DICT_WRITING[abs_path]:
                    pass
            except KeyError:
                pass
            if self.initialized:
                return

            default_logger.debug("Building prefix dict from %s ..." % (abs_path or 'the default dictionary'))
            t1 = time.time()
            if self.cache_file:
                cache_file = self.cache_file
            # default dictionary
            elif abs_path == DEFAULT_DICT:
                cache_file = "jieba.cache"
            # custom dictionary
            else:
                cache_file = "jieba.u%s.cache" % md5(
                    abs_path.encode('utf-8', 'replace')).hexdigest()
            cache_file = os.path.join(
                self.tmp_dir or tempfile.gettempdir(), cache_file)
            # prevent absolute path in self.cache_file
            tmpdir = os.path.dirname(cache_file)

            load_from_cache_fail = True
            if os.path.isfile(cache_file) and (abs_path == DEFAULT_DICT or
                os.path.getmtime(cache_file) > os.path.getmtime(abs_path)):
                default_logger.debug(
                    "Loading model from cache %s" % cache_file)
                try:
                    with open(cache_file, 'rb') as cf:
                        self.FREQ, self.total = marshal.load(cf)
                    load_from_cache_fail = False
                except Exception:
                    load_from_cache_fail = True

            if load_from_cache_fail:
                wlock = DICT_WRITING.get(abs_path, threading.RLock())
                DICT_WRITING[abs_path] = wlock
                with wlock:
                    self.FREQ, self.total = self.gen_pfdict(self.get_dict_file())
                    default_logger.debug(
                        "Dumping model to file cache %s" % cache_file)
                    try:
                        # prevent moving across different filesystems
                        fd, fpath = tempfile.mkstemp(dir=tmpdir)
                        with os.fdopen(fd, 'wb') as temp_cache_file:
                            marshal.dump(
                                (self.FREQ, self.total), temp_cache_file)
                        _replace_file(fpath, cache_file)
                    except Exception:
                        default_logger.exception("Dump cache file failed.")

                try:
                    del DICT_WRITING[abs_path]
                except KeyError:
                    pass

            self.initialized = True
            default_logger.debug(
                "Loading model cost %.3f seconds." % (time.time() - t1))
            default_logger.debug("Prefix dict has been built successfully.")

    def check_initialized(self):
        if not self.initialized:
            self.initialize()

    # 动态规划 计算最优路径
    def calc(self, sentence, DAG, route):
        N = len(sentence)
        route[N] = (0, 0)
        logtotal = log(self.total)
        # 从后往前遍历句子
        for idx in xrange(N - 1, -1, -1):
            # 对每个字计算最有可能的词组
            route[idx] = max((log(self.FREQ.get(sentence[idx:x + 1]) or 1) - logtotal + route[x + 1][0], x) for x in DAG[idx])

    # 输出有向无环图
    def get_DAG(self, sentence):
        self.check_initialized()
        DAG = {}
        N = len(sentence)
        for k in xrange(N):
            tmplist = []
            i = k
            # 提取字符段第k个字符
            frag = sentence[k]
            # 遍历查询dict.txt文件（349046行，第一列为词语，第二列为该词出现的频率，第三列为该词的词性）
            while i < N and frag in self.FREQ:
                if self.FREQ[frag]:
                    tmplist.append(i)
                i += 1
                frag = sentence[k:i + 1]
            if not tmplist:
                tmplist.append(k)
            DAG[k] = tmplist
        return DAG

    def __cut_all(self, sentence):
        dag = self.get_DAG(sentence)
        old_j = -1
        for k, L in iteritems(dag):
            if len(L) == 1 and k > old_j:
                yield sentence[k:L[0] + 1]
                old_j = L[0]
            else:
                for j in L:
                    if j > k:
                        yield sentence[k:j + 1]
                        old_j = j

    def __cut_DAG_NO_HMM(self, sentence):
        DAG = self.get_DAG(sentence)
        route = {}
        self.calc(sentence, DAG, route)
        x = 0
        N = len(sentence)
        buf = ''
        while x < N:
            y = route[x][1] + 1
            l_word = sentence[x:y]
            if re_eng.match(l_word) and len(l_word) == 1:
                buf += l_word
                x = y
            else:
                if buf:
                    yield buf
                    buf = ''
                yield l_word
                x = y
        if buf:
            yield buf
            buf = ''
    # HMM下使用的切词
    def __cut_DAG(self, sentence):
        # sentence：我来到北京清华大学
        # 输出对应的DAG图数据
        DAG = self.get_DAG(sentence)
        # {0: [0], 1: [1, 2], 2: [2], 3: [3, 4], 4: [4], 5: [5, 6, 8], 6: [6, 7], 7: [7, 8], 8: [8]}
        # DAG[5]=[5,6,8]的意思就是，以’清‘开头的话，分别以5、6、8结束时，可以是一个词语，即’清‘、’清华‘、’清华大学‘
        route = {}
        # 计算route
        self.calc(sentence, DAG, route)
        # route: 
        # {9: (0, 0), 8: (-8.142626068614787, 8), 7: (-8.006816355818659, 8), 6: (-17.53722513662092, 6), 5: (-11.085007904198626, 8), 4: (-20.20431518448597, 4), 3: (-18.548194315526874, 4), 2: (-24.22732015246924, 2), 1: (-27.379629658355885, 2), 0: (-32.587853155857076, 0)}
        x = 0
        buf = ''
        N = len(sentence)
        while x < N:
            y = route[x][1] + 1
            l_word = sentence[x:y]
            # 无法形成词的buf，交由HMM进行标注
            if y - x == 1:
                buf += l_word
            else:
                if buf:
                    if len(buf) == 1:
                        yield buf
                        buf = ''
                    else:
                        if not self.FREQ.get(buf):
                            # 当遇到一些dict.txt中没出现的词的时候，会进入这个函数
                            # 使用HMM的方法，对这些未识别成功的词进行标注
                            recognized = finalseg.cut(buf)
                            for t in recognized:
                                yield t
                        else:
                            for elem in buf:
                                yield elem
                        buf = ''
                yield l_word
            x = y

        if buf:
            if len(buf) == 1:
                yield buf
            elif not self.FREQ.get(buf):
                # 当遇到一些dict.txt中没出现的词的时候，会进入这个函数
                # 使用HMM的方法，对这些未识别成功的词进行标注
                print(buf)
                recognized = finalseg.cut(buf)
                print(recognized)
                for t in recognized:
                    yield t
            else:
                for elem in buf:
                    yield elem
    # 切词 cut方法 ，默认使用HMM隐马尔可夫模型  
    # 例子：sentence： 我来到北京清华大学，今天天气不错,good day!
    # 输出：我/来到/北京/清华大学/，/今天天气/不错/,/good/ /day/!
    def cut(self, sentence, cut_all=False, HMM=True):
        '''
        The main function that segments an entire sentence that contains
        Chinese characters into separated words.

        Parameter:
            - sentence: The str(unicode) to be segmented.
            - cut_all: Model type. True for full pattern, False for accurate pattern.
            - HMM: Whether to use the Hidden Markov Model.
        '''
        sentence = strdecode(sentence)

        if cut_all:
            re_han = re_han_cut_all
            re_skip = re_skip_cut_all
        else:
            re_han = re_han_default
            re_skip = re_skip_default
        if cut_all:
            cut_block = self.__cut_all
        elif HMM:
            cut_block = self.__cut_DAG
        else:
            cut_block = self.__cut_DAG_NO_HMM
        # 按正则先把成块的文本切开         
        # blocks： ['', '我来到北京清华大学', '，', '今天天气不错', ',', 'good', ' ', 'day', '!']
        blocks = re_han.split(sentence)
        for blk in blocks:
            if not blk:
                continue
            if re_han.match(blk):
                # 对文本块使用__cut_DAG进一步切词                
                for word in cut_block(blk):
                    yield word
            else:
                tmp = re_skip.split(blk)
                for x in tmp:
                    if re_skip.match(x):
                        yield x
                    elif not cut_all:
                        for xx in x:
                            yield xx
                    else:
                        yield x

    def cut_for_search(self, sentence, HMM=True):
        """
        Finer segmentation for search engines.
        """
        words = self.cut(sentence, HMM=HMM)
        for w in words:
            if len(w) > 2:
                for i in xrange(len(w) - 1):
                    gram2 = w[i:i + 2]
                    if self.FREQ.get(gram2):
                        yield gram2
            if len(w) > 3:
                for i in xrange(len(w) - 2):
                    gram3 = w[i:i + 3]
                    if self.FREQ.get(gram3):
                        yield gram3
            yield w

    def lcut(self, *args, **kwargs):
        return list(self.cut(*args, **kwargs))

    def lcut_for_search(self, *args, **kwargs):
        return list(self.cut_for_search(*args, **kwargs))

    _lcut = lcut
    _lcut_for_search = lcut_for_search

    def _lcut_no_hmm(self, sentence):
        return self.lcut(sentence, False, False)

    def _lcut_all(self, sentence):
        return self.lcut(sentence, True)

    def _lcut_for_search_no_hmm(self, sentence):
        return self.lcut_for_search(sentence, False)

    def get_dict_file(self):
        if self.dictionary == DEFAULT_DICT:
            return get_module_res(DEFAULT_DICT_NAME)
        else:
            return open(self.dictionary, 'rb')

    def load_userdict(self, f):
        '''
        Load personalized dict to improve detect rate.

        Parameter:
            - f : A plain text file contains words and their ocurrences.
                  Can be a file-like object, or the path of the dictionary file,
                  whose encoding must be utf-8.

        Structure of dict file:
        word1 freq1 word_type1
        word2 freq2 word_type2
        ...
        Word type may be ignored
        '''
        self.check_initialized()
        if isinstance(f, string_types):
            f_name = f
            f = open(f, 'rb')
        else:
            f_name = resolve_filename(f)
        for lineno, ln in enumerate(f, 1):
            line = ln.strip()
            if not isinstance(line, text_type):
                try:
                    line = line.decode('utf-8').lstrip('\ufeff')
                except UnicodeDecodeError:
                    raise ValueError('dictionary file %s must be utf-8' % f_name)
            if not line:
                continue
            # match won't be None because there's at least one character
            word, freq, tag = re_userdict.match(line).groups()
            if freq is not None:
                freq = freq.strip()
            if tag is not None:
                tag = tag.strip()
            self.add_word(word, freq, tag)

    def add_word(self, word, freq=None, tag=None):
        """
        Add a word to dictionary.

        freq and tag can be omitted, freq defaults to be a calculated value
        that ensures the word can be cut out.
        """
        self.check_initialized()
        word = strdecode(word)
        freq = int(freq) if freq is not None else self.suggest_freq(word, False)
        self.FREQ[word] = freq
        self.total += freq
        if tag:
            self.user_word_tag_tab[word] = tag
        for ch in xrange(len(word)):
            wfrag = word[:ch + 1]
            if wfrag not in self.FREQ:
                self.FREQ[wfrag] = 0
        if freq == 0:
            finalseg.add_force_split(word)

    def del_word(self, word):
        """
        Convenient function for deleting a word.
        """
        self.add_word(word, 0)

    def suggest_freq(self, segment, tune=False):
        """
        Suggest word frequency to force the characters in a word to be
        joined or splitted.

        Parameter:
            - segment : The segments that the word is expected to be cut into,
                        If the word should be treated as a whole, use a str.
            - tune : If True, tune the word frequency.

        Note that HMM may affect the final result. If the result doesn't change,
        set HMM=False.
        """
        self.check_initialized()
        ftotal = float(self.total)
        freq = 1
        if isinstance(segment, string_types):
            word = segment
            for seg in self.cut(word, HMM=False):
                freq *= self.FREQ.get(seg, 1) / ftotal
            freq = max(int(freq * self.total) + 1, self.FREQ.get(word, 1))
        else:
            segment = tuple(map(strdecode, segment))
            word = ''.join(segment)
            for seg in segment:
                freq *= self.FREQ.get(seg, 1) / ftotal
            freq = min(int(freq * self.total), self.FREQ.get(word, 0))
        if tune:
            self.add_word(word, freq)
        return freq

    def tokenize(self, unicode_sentence, mode="default", HMM=True):
        """
        Tokenize a sentence and yields tuples of (word, start, end)

        Parameter:
            - sentence: the str(unicode) to be segmented.
            - mode: "default" or "search", "search" is for finer segmentation.
            - HMM: whether to use the Hidden Markov Model.
        """
        if not isinstance(unicode_sentence, text_type):
            raise ValueError("jieba: the input parameter should be unicode.")
        start = 0
        if mode == 'default':
            for w in self.cut(unicode_sentence, HMM=HMM):
                width = len(w)
                yield (w, start, start + width)
                start += width
        else:
            for w in self.cut(unicode_sentence, HMM=HMM):
                width = len(w)
                if len(w) > 2:
                    for i in xrange(len(w) - 1):
                        gram2 = w[i:i + 2]
                        if self.FREQ.get(gram2):
                            yield (gram2, start + i, start + i + 2)
                if len(w) > 3:
                    for i in xrange(len(w) - 2):
                        gram3 = w[i:i + 3]
                        if self.FREQ.get(gram3):
                            yield (gram3, start + i, start + i + 3)
                yield (w, start, start + width)
                start += width

    def set_dictionary(self, dictionary_path):
        with self.lock:
            abs_path = _get_abs_path(dictionary_path)
            if not os.path.isfile(abs_path):
                raise Exception("jieba: file does not exist: " + abs_path)
            self.dictionary = abs_path
            self.initialized = False


# default Tokenizer instance

dt = Tokenizer()

# global functions

get_FREQ = lambda k, d=None: dt.FREQ.get(k, d)
add_word = dt.add_word
calc = dt.calc
cut = dt.cut
lcut = dt.lcut
cut_for_search = dt.cut_for_search
lcut_for_search = dt.lcut_for_search
del_word = dt.del_word
get_DAG = dt.get_DAG
get_dict_file = dt.get_dict_file
initialize = dt.initialize
load_userdict = dt.load_userdict
set_dictionary = dt.set_dictionary
suggest_freq = dt.suggest_freq
tokenize = dt.tokenize
user_word_tag_tab = dt.user_word_tag_tab


def _lcut_all(s):
    return dt._lcut_all(s)


def _lcut(s):
    return dt._lcut(s)


def _lcut_no_hmm(s):
    return dt._lcut_no_hmm(s)


def _lcut_all(s):
    return dt._lcut_all(s)


def _lcut_for_search(s):
    return dt._lcut_for_search(s)


def _lcut_for_search_no_hmm(s):
    return dt._lcut_for_search_no_hmm(s)


def _pcut(sentence, cut_all=False, HMM=True):
    parts = strdecode(sentence).splitlines(True)
    if cut_all:
        result = pool.map(_lcut_all, parts)
    elif HMM:
        result = pool.map(_lcut, parts)
    else:
        result = pool.map(_lcut_no_hmm, parts)
    for r in result:
        for w in r:
            yield w


def _pcut_for_search(sentence, HMM=True):
    parts = strdecode(sentence).splitlines(True)
    if HMM:
        result = pool.map(_lcut_for_search, parts)
    else:
        result = pool.map(_lcut_for_search_no_hmm, parts)
    for r in result:
        for w in r:
            yield w


def enable_parallel(processnum=None):
    """
    Change the module's `cut` and `cut_for_search` functions to the
    parallel version.

    Note that this only works using dt, custom Tokenizer
    instances are not supported.
    """
    global pool, dt, cut, cut_for_search
    from multiprocessing import cpu_count
    if os.name == 'nt':
        raise NotImplementedError(
            "jieba: parallel mode only supports posix system")
    else:
        from multiprocessing import Pool
    dt.check_initialized()
    if processnum is None:
        processnum = cpu_count()
    pool = Pool(processnum)
    cut = _pcut
    cut_for_search = _pcut_for_search


def disable_parallel():
    global pool, dt, cut, cut_for_search
    if pool:
        pool.close()
        pool = None
    cut = dt.cut
    cut_for_search = dt.cut_for_search
