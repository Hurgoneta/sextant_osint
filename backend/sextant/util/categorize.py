"""Turns raw search results into categories a normal person understands."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..registry.platforms import platform_for_host

CATEGORIES = {
    "social": "Social media",
    "forum": "Forums & communities",
    "news": "News articles",
    "document": "Documents",
    "code": "Code & developer sites",
    "data_broker": "People-search / data brokers",
    "business": "Business listings & registries",
    "image": "Images",
    "publication": "Publications & research",
    "archive": "Web archives",
    "website": "Websites & blogs",
    "other": "Other",
}

DOCUMENT_EXT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|csv|txt|odt|ods|rtf)(?:$|[?#])", re.I)

DATA_BROKERS = (
    "spokeo.com", "whitepages.com", "radaris.com", "beenverified.com", "pipl.com", "peekyou.com",
    "truepeoplesearch.com", "fastpeoplesearch.com", "mylife.com", "intelius.com", "instantcheckmate.com",
    "peoplefinders.com", "thatsthem.com", "zabasearch.com", "nuwber.com", "clustrmaps.com", "familytreenow.com",
    "usphonebook.com", "truthfinder.com", "192.com", "addresssearch.com", "rocketreach.co", "contactout.com",
    "signalhire.com", "lusha.com", "apollo.io", "zoominfo.com", "idcrawl.com", "sync.me", "truecaller.com",
)
BUSINESS = (
    "opencorporates.com", "crunchbase.com", "dnb.com", "bloomberg.com/profile", "yelp.com", "yellowpages.com",
    "paginiaurii.ro", "listafirme.ro", "termene.ro", "risco.ro", "companieshouse.gov.uk", "find-and-update.company-information.service.gov.uk",
    "northdata.com", "firmania.ro", "infobel.com", "europages.com", "kompass.com",
)
NEWS = (
    "nytimes.com", "bbc.co.uk", "bbc.com", "theguardian.com", "reuters.com", "apnews.com", "cnn.com",
    "washingtonpost.com", "digi24.ro", "hotnews.ro", "adevarul.ro", "libertatea.ro", "g4media.ro", "tion.ro",
    "pressalert.ro", "observator.tv", "stirileprotv.ro", "forbes.com", "medium.com/news", "spiegel.de",
    "lemonde.fr", "elpais.com", "corriere.it",
)
IMAGE_HOSTS = ("imgur.com", "flickr.com", "500px.com", "unsplash.com", "photobucket.com", "pinterest.com")
FORUM_HINTS = re.compile(r"(^|\.)(forum|forums|board|community|discuss|discourse)\.|/(forum|forums|thread|threads|topic|viewtopic|showthread|t)/", re.I)
NEWS_HINTS = re.compile(r"(^|\.)(news|stiri|ziar|gazeta|journal|times|post|herald|tribune)\.", re.I)


def _host_in(host: str, domains: tuple[str, ...], path: str = "") -> bool:
    target = host + path
    return any(host == d or host.endswith("." + d) or target.startswith(d) for d in domains)


def categorize_url(url: str, title: str = "") -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "other"
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or ""
    if DOCUMENT_EXT.search(path):
        return "document"
    if _host_in(host, DATA_BROKERS):
        return "data_broker"
    if _host_in(host, BUSINESS, path):
        return "business"
    if re.search(r"\.(jpe?g|png|gif|webp)$", path, re.I) or _host_in(host, IMAGE_HOSTS):
        return "image"
    platform = platform_for_host(host)
    if platform:
        if platform.category in ("developer", "packages"):
            return "code"
        if platform.category in ("forums", "qa"):
            return "forum"
        if platform.category in ("writing", "newsletters", "reference", "genealogy"):
            return "website"
        if platform.category == "academic":
            return "publication"
        if platform.category == "directories":
            return "business"
        return "social"
    if host.endswith("stackexchange.com") or host in ("stackoverflow.com", "superuser.com", "serverfault.com", "quora.com"):
        return "forum"
    if host in ("bitbucket.org", "sourceforge.net", "npmjs.com", "pypi.org", "gist.github.com", "huggingface.co"):
        return "code"
    if _host_in(host, NEWS) or NEWS_HINTS.search(host):
        return "news"
    if FORUM_HINTS.search(host) or FORUM_HINTS.search(path):
        return "forum"
    if host:
        return "website"
    return "other"
