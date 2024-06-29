# pylint:disable-msg=C0301,E0611,I1101
"""
Functions to process nodes in HTML code.
"""

import logging

from copy import deepcopy
import re

from courlan.urlutils import fix_relative_urls, get_base_url
from lxml.etree import Element, strip_tags, tostring

from .deduplication import duplicate_test
from .settings import CUT_EMPTY_ELEMS, MANUALLY_CLEANED, MANUALLY_STRIPPED
from .utils import textfilter, trim
from .xml import META_ATTRIBUTES


LOGGER = logging.getLogger(__name__)

REND_TAG_MAPPING = {
    'em': '#i',
    'i': '#i',
    'b': '#b',
    'strong': '#b',
    'u': '#u',
    'kbd': '#t',
    'samp': '#t',
    'tt': '#t',
    'var': '#t',
    'sub': '#sub',
    'sup': '#sup'
}

HTML_TAG_MAPPING = {v: k for k, v in REND_TAG_MAPPING.items()}


PRESERVE_IMG_CLEANING = {'figure', 'picture', 'source'}

MANUALLY_CLEANED_CHECK_CONTENT = ['aside', 'footer']


def delete_element(element):
    "Remove the element from the LXML tree."
    try:
        element.drop_tree()  # faster when applicable
    except AttributeError:  # pragma: no cover
        element.getparent().remove(element)

def check_preserve(element, patterns):
    """Check if the element or its descendants should be preserved based on patterns."""
    for text in element.itertext():
        if any(re.search(pattern, text) for pattern in patterns):
            return True
    return False

def mark_preserve(element):
    """Mark the element and its ancestors for preservation."""
    while element is not None and 'preserve-content' not in element.attrib:
        element.set('preserve-content', 'true')
        element = element.getparent()


def tree_cleaning(tree, options):
    "Prune the tree by discarding unwanted elements."
    # determine cleaning strategy, use lists to keep it deterministic
    cleaning_list, stripping_list = \
        MANUALLY_CLEANED.copy(), MANUALLY_STRIPPED.copy()
    if not options.tables:
        cleaning_list.extend(['table', 'td', 'th', 'tr'])
    else:
        # prevent this issue: https://github.com/adbar/trafilatura/issues/301
        for elem in tree.xpath('.//figure[descendant::table]'):
            elem.tag = 'div'
    if options.images:
        # Many websites have <img> inside <figure> or <picture> or <source> tag
        cleaning_list = [e for e in cleaning_list if e
                         not in PRESERVE_IMG_CLEANING]
        stripping_list.remove('img')
    
    if options.preserve:
        cleaning_list = [e for e in cleaning_list if e
                         not in MANUALLY_CLEANED_CHECK_CONTENT]

    # strip targeted elements
    strip_tags(tree, stripping_list)

    # prevent removal of paragraphs
    if options.focus == "recall" and tree.find('.//p') is not None:
        tcopy = deepcopy(tree)
        for expression in cleaning_list:
            for element in tree.getiterator(expression):
                delete_element(element)
        if tree.find('.//p') is None:
            tree = tcopy
    # delete targeted elements
    else:
        for expression in cleaning_list:
            for element in tree.getiterator(expression):
                delete_element(element)
    
    if options.preserve:
        # Collect and mark elements to preserve
        for element in tree.iter():
            if check_preserve(element, options.preserve):
                mark_preserve(element)
        
        # Remove non-preserved children of elements in MANUALLY_CLEANED_CHECK_CONTENT
        for expression in MANUALLY_CLEANED_CHECK_CONTENT:
            for element in tree.iter(expression):
                if 'preserve-content' not in element.attrib:
                    delete_element(element)
                else:
                    element.tag = 'div'
                    children = list(element)
                    for child in children:
                        if 'preserve-content' not in child.attrib:
                            delete_element(child)

    return prune_html(tree)


def prune_html(tree):
    "Delete selected empty elements to save space and processing time."
    # //comment() needed for date extraction
    for element in tree.xpath("//processing-instruction()|//*[not(node())]"):
        if element.tag in CUT_EMPTY_ELEMS:
            delete_element(element)
    return tree


def prune_unwanted_nodes(tree, nodelist, with_backup=False):
    '''Prune the HTML tree by removing unwanted sections.'''
    if with_backup:
        old_len = len(tree.text_content())  # ' '.join(tree.itertext())
        backup = deepcopy(tree)

    for expression in nodelist:
        for subtree in expression(tree):
            # preserve tail text from deletion
            if subtree.tail is not None:
                prev = subtree.getprevious()
                if prev is None:
                    prev = subtree.getparent()
                if prev is not None:
                    # There is a previous node, append text to its tail
                    prev.tail = (prev.tail or "") + " " + subtree.tail
            # remove the node
            if "preserve-content" not in subtree.attrib:
                subtree.getparent().remove(subtree)

    if with_backup:
        new_len = len(tree.text_content())
        # todo: adjust for recall and precision settings
        return tree if new_len > old_len/7 else backup
    return tree


def collect_link_info(links_xpath, favor_precision=False):
    '''Collect heuristics on link text'''
    # init
    mylist = []
    # longer strings impact recall in favor of precision
    threshold = 50 if favor_precision else 10
    # examine the elements
    for subelem in links_xpath:
        subelemtext = trim(subelem.text_content())
        if subelemtext:
            mylist.append(subelemtext)
    shortelems = sum(1 for text in mylist if len(text) < threshold)
    lengths = sum(len(text) for text in mylist)
    return lengths, len(mylist), shortelems, mylist


def link_density_test(element, text, favor_precision=False):
    '''Remove sections which are rich in links (probably boilerplate)'''
    links_xpath, mylist = element.findall('.//ref'), []
    if links_xpath:
        if element.tag == 'p': #  and not element.getparent().tag == 'item'
            if not favor_precision:
                if element.getnext() is None:
                    limitlen, threshold = 60, 0.8
                else:
                    limitlen, threshold = 30, 0.8
            else:
                limitlen, threshold = 200, 0.8
            #if 'hi' in list(element):
            #    limitlen, threshold = 100, 0.8
        #elif element.tag == 'head':
        #    limitlen, threshold = 50, 0.8
        else:
            if element.getnext() is None:
                limitlen, threshold = 300, 0.8
            #elif re.search(r'[.?!:]', elemtext):
            #    limitlen, threshold = 150, 0.66
            else:
                limitlen, threshold = 100, 0.8
        elemlen = len(text)
        if elemlen < limitlen:
            linklen, elemnum, shortelems, mylist = collect_link_info(links_xpath, favor_precision)
            if elemnum == 0:
                return True, mylist
            LOGGER.debug('list link text/total: %s/%s – short elems/total: %s/%s', linklen, elemlen, shortelems, elemnum)
            # (elemnum > 1 and shortelems/elemnum > 0.8):
            if linklen > threshold*elemlen or (elemnum > 1 and shortelems/elemnum > 0.8):
                return True, mylist
    return False, mylist


def link_density_test_tables(element):
    '''Remove tables which are rich in links (probably boilerplate)'''
    # if element.getnext() is not None:
    #     return False
    links_xpath = element.findall('.//ref')
    if links_xpath:
        elemlen = len(trim(element.text_content()))
        if elemlen > 250:
            linklen, elemnum, _, _ = collect_link_info(links_xpath)
            if elemnum == 0:
                return True
            LOGGER.debug('table link text: %s / total: %s', linklen, elemlen)
            return linklen > 0.8*elemlen if elemlen < 1000 else linklen > 0.5*elemlen
            # does more harm than good (issue #76)
            #if shortelems > len(links_xpath) * 0.66:
            #    return True
    return False


def delete_by_link_density(subtree, tagname, backtracking=False, favor_precision=False):
    '''Determine the link density of elements with respect to their length,
       and remove the elements identified as boilerplate.'''
    deletions = []
    threshold = 200 if favor_precision else 100

    for elem in subtree.iter(tagname):
        if 'preserve-content' in elem.attrib:
            continue
        elemtext = trim(elem.text_content())
        result, templist = link_density_test(elem, elemtext, favor_precision)
        if result:
            deletions.append(elem)
        elif backtracking and templist:  # if?
            if 0 < len(elemtext) < threshold and len(elem) >= 3:
                deletions.extend(elem)
                # print('backtrack:', text)
            # else: # and not re.search(r'[?!.]', text):
            # print(elem.tag, templist)

    for elem in dict.fromkeys(deletions):
        parent = elem.getparent()
        if parent is not None:
            parent.remove(elem)

    return subtree


def handle_textnode(elem, options, comments_fix=True, preserve_spaces=False):
    "Convert, format, and probe potential text elements."
    if elem.tag == "done" or (len(elem) == 0 and not elem.text and not elem.tail):
        return None

    # lb bypass
    if not comments_fix and elem.tag == "lb":
        if not preserve_spaces:
            elem.tail = trim(elem.tail)
        # if textfilter(elem) is True:
        #     return None
        # duplicate_test(subelement)?
        return elem

    if not elem.text and len(elem) == 0:
        # try the tail
        # LOGGER.debug('using tail for element %s', elem.tag)
        elem.text, elem.tail = elem.tail, ""
        # handle differently for br/lb
        if comments_fix and elem.tag == "lb":
            elem.tag = "p"

    # trim
    if not preserve_spaces:
        elem.text = trim(elem.text)
        if elem.tail:
            elem.tail = trim(elem.tail)

    # filter content
    # or not re.search(r'\w', element.text):  # text_content()?
    if not elem.text and textfilter(elem) or \
        (options.dedup and duplicate_test(elem, options)):
        return None
    return elem


def process_node(elem, options):
    "Convert, format, and probe potential text elements (light format)."
    if elem.tag == "done" or (len(elem) == 0 and not elem.text and not elem.tail):
        return None

    # trim
    elem.text, elem.tail = trim(elem.text), trim(elem.tail)

    # adapt content string
    if elem.tag != "lb" and not elem.text and elem.tail:
        elem.text, elem.tail = elem.tail, None

    # content checks
    if elem.text or elem.tail:
        if textfilter(elem) or (options.dedup and duplicate_test(elem, options)):
            return None

    return elem


def convert_lists(elem):
    "Convert <ul> and <ol> to <list> and underlying <li> elements to <item>."
    elem.set("rend", elem.tag)
    elem.tag = "list"
    i = 1
    for subelem in elem.iter("dd", "dt", "li"):
        # keep track of dd/dt items
        if subelem.tag in ("dd", "dt"):
            subelem.set("rend", f"{subelem.tag}-{i}")
            # increment counter after <dd> in description list
            if subelem.tag == "dd":
                i += 1
        # convert elem tag (needs to happen after the rest)
        subelem.tag = "item"

def convert_quotes(elem):
    "Convert quoted elements while accounting for nested structures."
    code_flag = False
    if elem.tag == "pre":
        # detect if there could be code inside
        children = elem.getchildren()
        # pre with a single span is more likely to be code
        if len(children) == 1 and children[0].tag == "span":
            code_flag = True
        # find hljs elements to detect if it's code
        code_elems = elem.xpath(".//span[starts-with(@class,'hljs')]")
        if code_elems:
            code_flag = True
            for subelem in code_elems:
                subelem.attrib.clear()
    elem.tag = "code" if code_flag else "quote"


def convert_headings(elem):
    "Add head tags and delete attributes."
    elem.attrib.clear()
    elem.set("rend", elem.tag)
    elem.tag = "head"


def convert_line_breaks(elem):
    "Convert <br> and <hr> to <lb>"
    elem.tag = "lb"


def convert_deletions(elem):
    'Convert <del>, <s>, <strike> to <del rend="overstrike">'
    elem.tag = "del"
    elem.set("rend", "overstrike")


def convert_details(elem):
    "Handle details and summary."
    elem.tag = "div"
    for subelem in elem.iter("summary"):
        subelem.tag = "head"


CONVERSIONS = {
    "dl": convert_lists, "ol": convert_lists, "ul": convert_lists,
    "h1": convert_headings, "h2": convert_headings, "h3": convert_headings,
    "h4": convert_headings, "h5": convert_headings, "h6": convert_headings,
    "br": convert_line_breaks, "hr": convert_line_breaks,
    "blockquote": convert_quotes, "pre": convert_quotes, "q": convert_quotes,
    "del": convert_deletions, "s": convert_deletions, "strike": convert_deletions,
    "details": convert_details,
    # wbr
}

def reorder_header_elements(tree):
    for ref in tree.xpath("//ref[head]"):
        head = ref.find("head")
        if head is not None:
            # Create new <ref> element to move inside <head>
            new_ref = Element(ref.tag, ref.attrib)
            new_ref.text = head.text

            # Create new <head> element and insert <ref> inside it
            new_head = Element(head.tag, head.attrib)
            new_head.append(new_ref)

            # Replace the old <ref> element with the new <head> element
            parent = ref.getparent()
            parent.replace(ref, new_head)

def convert_tags(tree, options, url=None):
    "Simplify markup and convert relevant HTML tags to an XML standard."
    # delete links for faster processing
    if not options.links:
        xpath_expr = './/div//a|.//ul//a'  # .//p//a ?
        if options.tables:
            xpath_expr += '|.//table//a'
        # necessary for further detection
        for elem in tree.xpath(xpath_expr):
            elem.tag = 'ref'
        # strip the rest
        strip_tags(tree, 'a')
    else:
        # get base URL for converting relative URLs
        base_url = url and get_base_url(url)
        for elem in tree.iter('a', 'ref'):
            elem.tag = 'ref'
            # replace href attribute and delete the rest
            target = elem.get('href') # defaults to None
            elem.attrib.clear()
            if target:
                # convert relative URLs
                if base_url:
                    target = fix_relative_urls(base_url, target)
                elem.set('target', target)

    if options.formatting:
        for elem in tree.iter(REND_TAG_MAPPING.keys()):
            elem.attrib.clear()
            elem.set('rend', REND_TAG_MAPPING[elem.tag])
            elem.tag = 'hi'
    else:
        strip_tags(tree, *REND_TAG_MAPPING.keys())

    # iterate over all concerned elements
    for elem in tree.iter(CONVERSIONS.keys()):
        CONVERSIONS[elem.tag](elem)
    # images
    if options.images:
        for elem in tree.iter('img'):
            elem.tag = 'graphic'
    reorder_header_elements(tree)
    return tree


HTML_CONVERSIONS = {
    "list": "ul",
    "item": "li",
    "code": "pre",
    "quote": "blockquote",
    "head": lambda elem: f"h{int(elem.get('rend')[1:])}",
    "lb": "br",
    "img": "graphic",
    "ref": "a",
    "hi": lambda elem: HTML_TAG_MAPPING[elem.get('rend')]
}


def convert_to_html(tree):
    "Convert XML to simplified HTML."
    for elem in tree.iter(HTML_CONVERSIONS.keys()):
        # apply function or straight conversion
        if callable(HTML_CONVERSIONS[elem.tag]):
            elem.tag = HTML_CONVERSIONS[elem.tag](elem)
        else:
            elem.tag = HTML_CONVERSIONS[elem.tag]
        # handle attributes
        if elem.tag == "a":
            elem.set("href", elem.get("target"))
            elem.attrib.pop("target")
        else:
            elem.attrib.clear()
    tree.tag = "body"
    root = Element("html")
    root.append(tree)
    return root


def build_html_output(document, with_metadata=False):
    "Convert the document to HTML and return a string."
    html_tree = convert_to_html(document.body)

    if with_metadata:
        head = Element("head")
        for item in META_ATTRIBUTES:
            value = getattr(document, item)
            if value:
                head.append(Element("meta", name=item, content=value))
        html_tree.insert(0, head)

    return tostring(html_tree, pretty_print=True, encoding='unicode').strip()
