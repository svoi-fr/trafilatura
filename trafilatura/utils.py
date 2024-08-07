# pylint:disable-msg=E0611,I1101
"""
Module bundling functions related to HTML and text processing,
content filtering and language detection.
"""

import gzip
import logging
import re
import zlib

from functools import lru_cache
from itertools import islice
from unicodedata import normalize

TARGET_ENCODINGS = [
    'utf-8',
    'windows-1251',  # Cyrillic
    'koi8-r',        # Cyrillic
    'iso-8859-5',    # Cyrillic
    'cp866',         # Cyrillic
    'windows-1256',  # Arabic
    'iso-8859-6'     # Arabic
]

# response compression
try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False

try:
    import zstandard
    HAS_ZSTD = True
except ImportError:
    HAS_ZSTD = False

# language detection
try:
    import py3langid
    LANGID_FLAG = True
except ImportError:
    LANGID_FLAG = False

# CChardet is faster and can be more accurate
try:
    from cchardet import detect as cchardet_detect
except ImportError:
    cchardet_detect = None
from charset_normalizer import from_bytes
from lxml.html import HtmlElement, HTMLParser, fromstring
# response types
from urllib3.response import HTTPResponse


LOGGER = logging.getLogger(__name__)

UNICODE_ALIASES = {'utf-8', 'utf_8'}

DOCTYPE_TAG = re.compile("^< ?! ?DOCTYPE.+?/ ?>", re.I)
FAULTY_HTML = re.compile(r"(<html.*?)\s*/>", re.I)
HTML_STRIP_TAGS = re.compile(r'(<!--.*?-->|<[^>]*>)')

# note: htmldate could use HTML comments
# huge_tree=True, remove_blank_text=True
HTML_PARSER = HTMLParser(collect_ids=False, default_doctype=False, encoding='utf-8', remove_comments=True, remove_pis=True)

LINES_TRIMMING = re.compile(r'(?<![p{P}>])\n', flags=re.UNICODE|re.MULTILINE)

URL_BLACKLIST_REGEX = re.compile(r'^https?://|/+$')

# Regex to check image file extensions
IMAGE_EXTENSION = re.compile(r'[^\s]+\.(avif|bmp|gif|hei[cf]|jpe?g|png|webp)(\b|$)')

FORMATTING_PROTECTED = {'cell', 'head', 'hi', 'item', 'p', 'quote', 'ref', 'td'}
SPACING_PROTECTED = {'code', 'pre'}

# https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Content-Language
TARGET_LANG_ATTRS = ('http-equiv="content-language"', 'property="og:locale"')
RE_HTML_LANG = re.compile(r'([a-z]{2})')

# Mostly filters for social media
RE_FILTER = re.compile(r'\W*(Drucken|E-?Mail|Facebook|Flipboard|Google|Instagram|'
                        'Linkedin|Mail|PDF|Pinterest|Pocket|Print|QQ|Reddit|Twitter|'
                        'WeChat|WeiBo|Whatsapp|Xing|Mehr zum Thema:?|More on this.{,8}$)$',
                       flags=re.IGNORECASE)
# COMMENTS_BLACKLIST = ('( Abmelden / Ändern )') # Fill in your details below|Trage deine Daten unten|Kommentar verfassen|Bitte logge dich|Hinterlasse einen Kommentar| to %s| mit %s)


def handle_compressed_file(filecontent):
    """
    Don't trust response headers and try to decompress a binary string
    with a cascade of installed packages. Use magic numbers when available.
    """
    if not isinstance(filecontent, bytes):
        return filecontent

    # source: https://stackoverflow.com/questions/3703276/how-to-tell-if-a-file-is-gzip-compressed
    if filecontent[:3] == b"\x1f\x8b\x08":
        try:
            return gzip.decompress(filecontent)
        except Exception:  # EOFError, OSError, gzip.BadGzipFile
            LOGGER.warning("invalid GZ file")
    # try zstandard
    if HAS_ZSTD and filecontent[:4] == b"\x28\xb5\x2f\xfd":
        try:
            return zstandard.decompress(filecontent)  # max_output_size=???
        except zstandard.ZstdError:
            LOGGER.warning("invalid ZSTD file")
    # try brotli
    if HAS_BROTLI:
        try:
            return brotli.decompress(filecontent)
        except brotli.error:
            pass  # logging.debug('invalid Brotli file')
    # try zlib/deflate
    try:
        return zlib.decompress(filecontent)
    except zlib.error:
        pass

    # return content unchanged if decompression failed
    return filecontent


def isutf8(data):
    """Simple heuristic to determine if a bytestring uses standard unicode encoding"""
    try:
        data.decode('UTF-8')
    except UnicodeDecodeError:
        return False
    return True

def is_utf8(data, chunk_size=1024, sample_rate=0.1):
    """Check if data is utf-8 encoded by decoding in chunks."""
    total_chunks = len(data) // chunk_size
    if total_chunks == 0:
        total_chunks = 1
    sample_chunks = max(int(total_chunks * sample_rate), 1)
    for i in range(sample_chunks):
        start = (len(data) // sample_chunks) * i
        end = start + chunk_size
        try:
            data[start:end].decode('utf-8')
        except UnicodeDecodeError:
            return False
    return True

def detect_encoding(bytesobject):
    """Read all input or first chunk and return a list of encodings"""
    guesses = []

    # Check if it's UTF-8
    if is_utf8(bytesobject):
        return ['utf-8']

    # Use charset_normalizer for further guesses
    if len(bytesobject) < 10000:
        detection_results = from_bytes(bytesobject)
    else:
        detection_results = from_bytes(bytesobject[:5000] + bytesobject[-5000:]) or \
                            from_bytes(bytesobject)
    if len(detection_results) > 0:
        guesses.extend([r.encoding for r in detection_results])

    # If guesses include UTF-8, prioritize it
    if 'utf-8' in guesses:
        return ['utf-8']

    # Filter out unwanted aliases and return the guesses
    return [g for g in guesses if g not in UNICODE_ALIASES]


def decode_response(content):
    """Read the urllib3 object corresponding to the server response,
       try to guess its encoding and decode it to return a unicode string"""
    raise ValueError("decode_response() is deprecated, use decode_file() instead.")


def decode_file(filecontent):
    """Check if the bytestring could be GZip and eventually decompress it,
       guess bytestring encoding and try to decode to Unicode string.
       Resort to destructive conversion otherwise."""
    # init
    if isinstance(filecontent, str):
        return filecontent
    htmltext = None
    # GZip and Brotli test
    filecontent = handle_compressed_file(filecontent)
    # encoding
    for guessed_encoding in detect_encoding(filecontent):
        try:
            htmltext = filecontent.decode(guessed_encoding)
        except (LookupError, UnicodeDecodeError): # VISCII: lookup
            LOGGER.warning('wrong encoding detected: %s', guessed_encoding)
            htmltext = None
        else:
            break
    # return original content if nothing else succeeded
    return htmltext or str(filecontent, encoding='utf-8', errors='replace')


def is_dubious_html(beginning: str) -> bool:
    "Assess if the object is proper HTML (awith a corresponding tag or declaration)."
    return "html" not in beginning


def repair_faulty_html(htmlstring: str, beginning: str) -> str:
    "Repair faulty HTML strings to make then palatable for libxml2."
    # libxml2/LXML issue: https://bugs.launchpad.net/lxml/+bug/1955915
    if "doctype" in beginning:
        firstline, _, rest = htmlstring.partition("\n")
        htmlstring = DOCTYPE_TAG.sub("", firstline, count=1) + "\n" + rest
    # other issue with malformed documents: check first three lines
    for i, line in enumerate(iter(htmlstring.splitlines())):
        if "<html" in line and line.endswith("/>"):
            htmlstring = FAULTY_HTML.sub(r"\1>", htmlstring, count=1)
            break
        if i > 2:
            break
    return htmlstring


def fromstring_bytes(htmlobject):
    "Try to pass bytes to LXML parser."
    tree = None
    try:
        tree = fromstring(htmlobject.encode("utf8", "surrogatepass"), parser=HTML_PARSER)
    except Exception as err:
        LOGGER.error("lxml parser bytestring %s", err)
    return tree


def load_html(htmlobject):
    """Load object given as input and validate its type
    (accepted: lxml.html tree, trafilatura/urllib3 response, bytestring and string)
    """
    # use tree directly
    if isinstance(htmlobject, HtmlElement):
        return htmlobject
    # use trafilatura or urllib3 responses directly
    if isinstance(htmlobject, HTTPResponse) or hasattr(htmlobject, "data"):
        htmlobject = htmlobject.data
    # do not accept any other type after this point
    if not isinstance(htmlobject, (bytes, str)):
        raise TypeError("incompatible input type", type(htmlobject))
    # start processing
    tree = None
    # try to guess encoding and decode file: if None then keep original
    htmlobject = decode_file(htmlobject)
    # sanity checks
    beginning = htmlobject[:50].lower()
    check_flag = is_dubious_html(beginning)
    # repair first
    htmlobject = repair_faulty_html(htmlobject, beginning)
    # first pass: use Unicode string
    fallback_parse = False
    try:
        tree = fromstring(htmlobject, parser=HTML_PARSER)
    except ValueError:
        # "Unicode strings with encoding declaration are not supported."
        tree = fromstring_bytes(htmlobject)
        fallback_parse = True
    except Exception as err:  # pragma: no cover
        LOGGER.error("lxml parsing failed: %s", err)
    # second pass: try passing bytes to LXML
    if (tree is None or len(tree) < 1) and not fallback_parse:
        tree = fromstring_bytes(htmlobject)
    # rejection test: is it (well-formed) HTML at all?
    # log parsing errors
    if tree is not None and check_flag is True and len(tree) < 2:
        LOGGER.error(
            "parsed tree length: %s, wrong data type or not valid HTML", len(tree)
        )
        tree = None
    return tree


@lru_cache(maxsize=2**14)  # sys.maxunicode = 1114111
def return_printables_and_spaces(char):
    'Return a character if it belongs to certain classes'
    return char if char.isprintable() or char.isspace() else ''


def remove_control_characters(string):
    '''Prevent non-printable and XML invalid character errors'''
    return ''.join(map(return_printables_and_spaces, string))


def normalize_unicode(string, unicodeform='NFC'):
    'Normalize the given string to the specified unicode format.'
    return normalize(unicodeform, string)


@lru_cache(maxsize=1024)
def line_processing(line, preserve_space=False, trailing_space=False):
    '''Remove HTML space entities, then discard incompatible unicode
       and invalid XML characters on line level'''
    # spacing HTML entities: https://www.w3.org/MarkUp/html-spec/html-spec_13.html
    # unique code spaces
    new_line = remove_control_characters(line.replace('&#13;', '\r').replace('&#10;', '\n').replace('&nbsp;', '\u00A0'))
    if not preserve_space:
        # remove newlines that are not related to punctuation or markup
        # remove non-printable chars and normalize space characters (including Unicode spaces)
        new_line = trim(LINES_TRIMMING.sub(r" ", new_line))
        # prune empty lines
        if all(map(str.isspace, new_line)):
            new_line = None
        elif trailing_space:
            space_before = " " if line[0].isspace() else ""
            space_after = " " if line[-1].isspace() else ""
            new_line = "".join([space_before, new_line, space_after])
    return new_line


def sanitize(text, preserve_space=False, trailing_space=False):
    '''Convert text and discard incompatible and invalid characters'''
    # consider all text as a single line
    if trailing_space:
        return line_processing(text, preserve_space, True)
    # process line by line
    try:
        return '\n'.join(filter(None, (line_processing(l, preserve_space) for l in text.splitlines()))).replace('\u2424', '')
    except AttributeError:
        return None


def sanitize_tree(tree):
    '''Trims spaces, removes control characters and normalizes unicode'''
    for elem in tree.iter():
        parent = elem.getparent()
        parent_tag = parent.tag if parent is not None else ""

        # preserve space if the element or its parent is a specific tag, or if the element has text and children
        # the last part is relevant for item elements with ref inside for example
        preserve_space = elem.tag in SPACING_PROTECTED or parent_tag in SPACING_PROTECTED
        trailing_space = elem.tag in FORMATTING_PROTECTED or parent_tag in FORMATTING_PROTECTED or preserve_space

        # remove invalid attributes
        for attribute in elem.attrib:
            if ':' in attribute:  # colon is reserved for namespaces in XML
                if not elem.attrib[attribute] or attribute.split(':', 1)[0] not in tree.nsmap:
                    elem.attrib.pop(attribute)

        if elem.text:
            elem.text = sanitize(elem.text, preserve_space, trailing_space)
        if elem.tail:
            elem.tail = sanitize(elem.tail, preserve_space, trailing_space)
    return tree


@lru_cache(maxsize=1024)
def trim(string):
    '''Remove unnecessary spaces within a text string'''
    try:
        # remove newlines that are not related to punctuation or markup + proper trimming
        # return LINES_TRIMMING.sub(r' ', string).strip(' \t\n\r\v')
        # faster:
        return ' '.join(string.split()).strip()
    except (AttributeError, TypeError):
        return None


def is_image_file(imagesrc):
    '''Check if the observed string corresponds to a valid image extension,
       return False otherwise'''
    return bool(imagesrc is not None and IMAGE_EXTENSION.search(imagesrc))


def make_chunks(iterable, n):
    """
    Chunk data into smaller pieces.
    https://docs.python.org/3/library/itertools.html
    """
    it = iter(iterable)
    while True:
        chunk = tuple(islice(it, n))
        if not chunk:
            return
        yield chunk
    # Python 3.8+ with walrus operator
    # while batch := tuple(islice(it, n)):
    #    yield batch


def is_acceptable_length(my_len, options):
    "Check if the document length is within acceptable boundaries."
    if my_len < options.min_file_size:
        LOGGER.error("too small/incorrect for URL %s", options.url)
        return False
    if my_len > options.max_file_size:
        LOGGER.error("too large: length %s for URL %s", my_len, options.url)
        return False
    return True


def check_html_lang(tree, target_language, strict=False):
    """Check HTML meta-elements for language information and split
       the result in case there are several languages."""
    for attr in TARGET_LANG_ATTRS:
        elems = tree.findall(f'.//meta[@{attr}][@content]')
        if elems:
            if any(target_language in RE_HTML_LANG.split(elem.get("content", "").lower()) for elem in elems):
                return True
            LOGGER.debug("%s lang attr failed", attr)
            return False

    # HTML lang attribute: sometimes a wrong indication
    if strict:
        elems = tree.xpath("//html[@lang]")
        if elems:
            if any(target_language in RE_HTML_LANG.split(elem.get("lang", "").lower()) for elem in elems):
                return True
            LOGGER.debug("HTML lang failed")
            return False

    LOGGER.debug("No relevant lang elements found")
    return True


def language_classifier(temp_text, temp_comments):
    '''Run external component (if installed) for language identification'''
    if LANGID_FLAG is True:
        result, _ = (
            py3langid.classify(temp_text)
            if len(temp_text) > len(temp_comments)
            else py3langid.classify(temp_comments)
        )
    else:
        LOGGER.warning('Language detector not installed, skipping detection')
        result = None
    return result


def language_filter(temp_text, temp_comments, target_language, docmeta):
    '''Filter text based on language detection and store relevant information'''
    # todo: run and pass info along anyway?
    if target_language is not None:
        # more thorough: detection on actual text content
        docmeta.language = language_classifier(temp_text, temp_comments)
        # HTML lang check? sometimes contradicted by detection above
        #if docmeta.language is None:
        #    if check_html_lang(tree, target_language) is False:
        #        LOGGER.error('wrong HTML meta language for URL %s', url)
        #        raise ValueError
        if docmeta.language is not None and docmeta.language != target_language:
            LOGGER.warning('wrong language: %s %s', docmeta.language, docmeta.url)
            return True, docmeta
    return False, docmeta


def textfilter(element):
    '''Filter out unwanted text'''
    testtext = element.tail if element.text is None else element.text
    # to check: line len → continue if len(line) <= 5
    return not text_chars_test(testtext) or any(map(RE_FILTER.match, testtext.splitlines()))


def text_chars_test(string):
    '''Determine if a string is only composed of spaces and/or control characters'''
    # or not re.search(r'\w', string)
    # return string is not None and len(string) != 0 and not string.isspace()
    return bool(string) and not string.isspace()
