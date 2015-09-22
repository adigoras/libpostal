# -*- coding: utf-8 -*-
'''
osm_address_training_data.py
----------------------------

This script generates several training sets from OpenStreetMap addresses,
streets, venues and toponyms.

Note: the combined size of all the files created by this script exceeds 100GB
so if training these models, it is wise to use a server-grade machine with
plenty of disk space. The following commands can be used in parallel to create 
all the training sets:

Ways:
python osm_address_training_data.py -s $(OSM_DIR)/planet-ways.osm --rtree-dir=$(RTREE_DIR) -o $(OUT_DIR)

Venues:
python osm_address_training_data.py -v $(OSM_DIR)/planet-venues.osm --rtree-dir=$(RTREE_DIR) -o $(OUT_DIR)

Address streets:
python osm_address_training_data.py -a $(OSM_DIR)/planet-addresses.osm --rtree-dir=$(RTREE_DIR) -o $(OUT_DIR)

Limited formatted addresses:
python osm_address_training_data.py -a -l $(OSM_DIR)/planet-addresses.osm --rtree-dir=$(RTREE_DIR) -o $(OUT_DIR)

Formatted addresses (tagged):
python osm_address_training_data.py -a -f $(OSM_DIR)/planet-addresses.osm --rtree-dir=$(RTREE_DIR) -o $(OUT_DIR)

Formatted addresses (untagged):
python osm_address_training_data.py -a -f -u $(OSM_DIR)/planet-addresses.osm --rtree-dir=$(RTREE_DIR) -o $(OUT_DIR)

Toponyms:
python osm_address_training_data.py -b $(OSM_DIR)/planet-borders.osm --rtree-dir=$(RTREE_DIR) -o $(OUT_DIR)
'''

import argparse
import csv
import os
import operator
import re
import sys
import tempfile
import urllib
import ujson as json
import HTMLParser

from collections import defaultdict, OrderedDict
from lxml import etree
from itertools import ifilter, chain

this_dir = os.path.realpath(os.path.dirname(__file__))
sys.path.append(os.path.realpath(os.path.join(os.pardir, os.pardir)))

sys.path.append(os.path.realpath(os.path.join(os.pardir, os.pardir, os.pardir, 'python')))

from geodata.language_id.disambiguation import *
from geodata.language_id.polygon_lookup import country_and_languages
from geodata.i18n.languages import *
from geodata.address_formatting.formatter import AddressFormatter
from geodata.polygons.language_polys import *
from geodata.i18n.unicode_paths import DATA_DIR

from geodata.csv_utils import *
from geodata.file_utils import *

this_dir = os.path.realpath(os.path.dirname(__file__))

WAY_OFFSET = 10 ** 15
RELATION_OFFSET = 2 * 10 ** 15

# Input files
PLANET_ADDRESSES_INPUT_FILE = 'planet-addresses.osm'
PLANET_WAYS_INPUT_FILE = 'planet-ways.osm'
PLANET_VENUES_INPUT_FILE = 'planet-venues.osm'
PLANET_BORDERS_INPUT_FILE = 'planet-borders.osm'

ALL_OSM_TAGS = set(['node', 'way', 'relation'])
WAYS_RELATIONS = set(['way', 'relation'])

# Output files
WAYS_LANGUAGE_DATA_FILENAME = 'streets_by_language.tsv'
ADDRESS_LANGUAGE_DATA_FILENAME = 'address_streets_by_language.tsv'
ADDRESS_FORMAT_DATA_TAGGED_FILENAME = 'formatted_addresses_tagged.tsv'
ADDRESS_FORMAT_DATA_FILENAME = 'formatted_addresses.tsv'
ADDRESS_FORMAT_DATA_LANGUAGE_FILENAME = 'formatted_addresses_by_language.tsv'
TOPONYM_LANGUAGE_DATA_FILENAME = 'toponyms_by_language.tsv'


class OSMField(object):
    def __init__(self, name, c_constant, alternates=None):
        self.name = name
        self.c_constant = c_constant
        self.alternates = alternates

osm_fields = [
    # Field if alternate_names present, default field name if not, C header constant
    OSMField('addr:housename', 'OSM_HOUSE_NAME'),
    OSMField('addr:housenumber', 'OSM_HOUSE_NUMBER'),
    OSMField('addr:block', 'OSM_BLOCK'),
    OSMField('addr:street', 'OSM_STREET_ADDRESS'),
    OSMField('addr:place', 'OSM_PLACE'),
    OSMField('addr:city', 'OSM_CITY', alternates=['addr:locality', 'addr:municipality', 'addr:hamlet']),
    OSMField('addr:suburb', 'OSM_SUBURB'),
    OSMField('addr:neighborhood', 'OSM_NEIGHBORHOOD', alternates=['addr:neighbourhood']),
    OSMField('addr:district', 'OSM_DISTRICT'),
    OSMField('addr:subdistrict', 'OSM_SUBDISTRICT'),
    OSMField('addr:ward', 'OSM_WARD'),
    OSMField('addr:state', 'OSM_STATE'),
    OSMField('addr:province', 'OSM_PROVINCE'),
    OSMField('addr:postcode', 'OSM_POSTAL_CODE', alternates=['addr:postal_code']),
    OSMField('addr:country', 'OSM_COUNTRY'),
]


# Currently, all our data sets are converted to nodes with osmconvert before parsing
def parse_osm(filename, allowed_types=ALL_OSM_TAGS):
    f = open(filename)
    parser = etree.iterparse(f)

    single_type = len(allowed_types) == 1

    for (_, elem) in parser:
        elem_id = long(elem.attrib.pop('id', 0))
        item_type = elem.tag
        if elem_id >= WAY_OFFSET and elem_id < RELATION_OFFSET:
            elem_id -= WAY_OFFSET
            item_type = 'way'
        elif elem_id >= RELATION_OFFSET:
            elem_id -= RELATION_OFFSET
            item_type = 'relation'

        if item_type in allowed_types:
            attrs = OrderedDict(elem.attrib)
            attrs.update(OrderedDict([(e.attrib['k'], e.attrib['v'])
                         for e in elem.getchildren() if e.tag == 'tag']))
            key = elem_id if single_type else '{}:{}'.format(item_type, elem_id)
            yield key, attrs

        if elem.tag != 'tag':
            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]


def write_osm_json(filename, out_filename):
    out = open(out_filename, 'w')
    writer = csv.writer(out, 'tsv_no_quote')
    for key, attrs in parse_osm(filename):
        writer.writerow((key, json.dumps(attrs)))
    out.close()


def read_osm_json(filename):
    reader = csv.reader(open(filename), delimiter='\t')
    for key, attrs in reader:
        yield key, json.loads(attrs)



def normalize_osm_name_tag(tag, script=False):
    norm = tag.rsplit(':', 1)[-1]
    if not script:
        return norm
    return norm.split('_', 1)[0]


beginning_re = re.compile('^[^0-9\-]+', re.UNICODE)
end_re = re.compile('[^0-9]+$', re.UNICODE)

latitude_dms_regex = re.compile(ur'^(-?[0-9]{1,2})[ ]*[ :°ºd][ ]*([0-5]?[0-9])?[ ]*[:\'\u2032m]?[ ]*([0-5]?[0-9](?:\.\d+)?)?[ ]*[:\?\"\u2033s]?[ ]*(N|n|S|s)?$', re.I | re.UNICODE)
longitude_dms_regex = re.compile(ur'^(-?1[0-8][0-9]|0?[0-9]{1,2})[ ]*[ :°ºd][ ]*([0-5]?[0-9])?[ ]*[:\'\u2032m]?[ ]*([0-5]?[0-9](?:\.\d+)?)?[ ]*[:\?\"\u2033s]?[ ]*(E|e|W|w)?$', re.I | re.UNICODE)

latitude_decimal_with_direction_regex = re.compile('^(-?[0-9][0-9](?:\.[0-9]+))[ ]*[ :°ºd]?[ ]*(N|n|S|s)$', re.I)
longitude_decimal_with_direction_regex = re.compile('^(-?1[0-8][0-9]|0?[0-9][0-9](?:\.[0-9]+))[ ]*[ :°ºd]?[ ]*(E|e|W|w)$', re.I)


def latlon_to_floats(latitude, longitude):
    have_lat = False
    have_lon = False

    latitude = safe_decode(latitude).strip(u' ,;|')
    longitude = safe_decode(longitude).strip(u' ,;|')

    latitude = latitude.replace(u',', u'.')
    longitude = longitude.replace(u',', u'.')

    lat_dms = latitude_dms_regex.match(latitude)
    lat_dir = latitude_decimal_with_direction_regex.match(latitude)

    if lat_dms:
        d, m, s, c = lat_dms.groups()
        sign = direction_sign(c)
        latitude = degrees_to_decimal(d or 0, m or 0, s or 0)
        have_lat = True
    elif lat_dir:
        d, c = lat_dir.groups()
        sign = direction_sign(c)
        latitude = float(d) * sign
        have_lat = True
    else:
        latitude = re.sub(beginning_re, u'', latitude)
        latitude = re.sub(end_re, u'', latitude)

    lon_dms = longitude_dms_regex.match(longitude)
    lon_dir = longitude_decimal_with_direction_regex.match(longitude)

    if lon_dms:
        d, m, s, c = lon_dms.groups()
        sign = direction_sign(c)
        longitude = degrees_to_decimal(d or 0, m or 0, s or 0)
        have_lon = True
    elif lon_dir:
        d, c = lon_dir.groups()
        sign = direction_sign(c)
        longitude = float(d) * sign
        have_lon = True
    else:
        longitude = re.sub(beginning_re, u'', longitude)
        longitude = re.sub(end_re, u'', longitude)

    return float(latitude), float(longitude)


def get_language_names(language_rtree, key, value, tag_prefix='name'):
    if not ('lat' in value and 'lon' in value):
        return None, None

    has_colon = ':' in tag_prefix
    tag_first_component = tag_prefix.split(':')[0]
    tag_last_component = tag_prefix.split(':')[-1]

    try:
        latitude, longitude = latlon_to_floats(value['lat'], value['lon'])
    except Exception:
        return None, None

    country, candidate_languages, language_props = country_and_languages(language_rtree, latitude, longitude)
    if not (country and candidate_languages):
        return None, None

    num_langs = len(candidate_languages)
    default_langs = set([l['lang'] for l in candidate_languages if l.get('default')])
    num_defaults = len(default_langs)
    name_language = defaultdict(list)

    alternate_langs = []

    equivalent_alternatives = defaultdict(list)
    for k, v in value.iteritems():
        if k.startswith(tag_prefix + ':') and normalize_osm_name_tag(k, script=True) in languages:
            lang = k.rsplit(':', 1)[-1]
            alternate_langs.append((lang, v))
            equivalent_alternatives[v].append(lang)

    has_alternate_names = len(alternate_langs)
    # Some countries like Lebanon list things like name:en == name:fr == "Rue Abdel Hamid Karame"
    # Those addresses should be disambiguated rather than taken for granted
    ambiguous_alternatives = set([k for k, v in equivalent_alternatives.iteritems() if len(v) > 1])

    regional_defaults = 0
    country_defaults = 0
    regional_langs = set()
    country_langs = set()
    for p in language_props:
        if p['admin_level'] > 0:
            regional_defaults += sum((1 for lang in p['languages'] if lang.get('default')))
            regional_langs |= set([l['lang'] for l in p['languages']])
        else:
            country_defaults += sum((1 for lang in p['languages'] if lang.get('default')))
            country_langs |= set([l['lang'] for l in p['languages']])

    ambiguous_already_seen = set()

    for k, v in value.iteritems():
        if k.startswith(tag_prefix + ':'):
            if v not in ambiguous_alternatives:
                norm = normalize_osm_name_tag(k)
                norm_sans_script = normalize_osm_name_tag(k, script=True)
                if norm in languages or norm_sans_script in languages:
                    name_language[norm].append(v)
            elif v not in ambiguous_already_seen:
                langs = [(lang, lang in default_langs) for lang in equivalent_alternatives[v]]
                lang = disambiguate_language(v, langs)

                if lang != AMBIGUOUS_LANGUAGE and lang != UNKNOWN_LANGUAGE:
                    name_language[lang].append(v)

                ambiguous_already_seen.add(v)
        elif not has_alternate_names and k.startswith(tag_first_component) and (has_colon or ':' not in k) and normalize_osm_name_tag(k, script=True) == tag_last_component:
            if num_langs == 1:
                name_language[candidate_languages[0]['lang']].append(v)
            else:
                lang = disambiguate_language(v, [(l['lang'], l['default']) for l in candidate_languages])
                default_lang = candidate_languages[0]['lang']

                if lang == AMBIGUOUS_LANGUAGE:
                    return None, None
                elif lang == UNKNOWN_LANGUAGE and num_defaults == 1:
                    name_language[default_lang].append(v)
                elif lang != UNKNOWN_LANGUAGE:
                    if lang != default_lang and lang in country_langs and country_defaults > 1 and regional_defaults > 0 and lang in WELL_REPRESENTED_LANGUAGES:
                        return None, None
                    name_language[lang].append(v)
                else:
                    return None, None

    return country, name_language


def build_ways_training_data(language_rtree, infile, out_dir):
    '''
    Creates a training set for language classification using most OSM ways
    (streets) under a fairly lengthy osmfilter definition which attempts to
    identify all roads/ways designated for motor vehicle traffic, which
    is more-or-less what we'd expect to see in addresses.

    The fields are {language, country, street name}. Example:

    ar      ma      ﺵﺍﺮﻋ ﻑﺎﻟ ﻮﻟﺩ ﻊﻤﻳﺭ
    '''
    i = 0
    f = open(os.path.join(out_dir, WAYS_LANGUAGE_DATA_FILENAME), 'w')
    writer = csv.writer(f, 'tsv_no_quote')

    for key, value in parse_osm(infile, allowed_types=WAYS_RELATIONS):
        country, name_language = get_language_names(language_rtree, key, value, tag_prefix='name')
        if not name_language:
            continue

        for k, v in name_language.iteritems():
            for s in v:
                if k in languages:
                    writer.writerow((k, country, tsv_string(s)))
            if i % 1000 == 0 and i > 0:
                print 'did', i, 'ways'
            i += 1
    f.close()

OSM_IGNORE_KEYS = (
    'house',
)


def strip_keys(value, ignore_keys):
    for key in ignore_keys:
        value.pop(key, None)


def build_address_format_training_data(language_rtree, infile, out_dir, tag_components=True):
    '''
    Creates formatted address training data for supervised sequence labeling (or potentially 
    for unsupervised learning e.g. for word vectors) using addr:* tags in OSM.

    Example:

    cs  cz  Gorkého/road ev.2459/house_number | 40004/postcode Trmice/city | CZ/country

    The field structure is similar to other training data created by this script i.e.
    {language, country, data}. The data field here is a sequence of labeled tokens similar
    to what we might see in part-of-speech tagging.

    This format uses a special character "|" to denote possible breaks in the input (comma, newline).
    This information can potentially be used downstream by the sequence model as these
    breaks may be present at prediction time.

    Example:

    sr      rs      Crkva Svetog Arhangela Mihaila | Vukov put BB | 15303 Trsic

    This may be useful in learning word representations, statistical phrases, morphology
    or other models requiring only the sequence of words.
    '''
    i = 0

    formatter = AddressFormatter()

    if tag_components:
        formatted_tagged_file = open(os.path.join(out_dir, ADDRESS_FORMAT_DATA_TAGGED_FILENAME), 'w')
        writer = csv.writer(formatted_tagged_file, 'tsv_no_quote')
    else:
        formatted_file = open(os.path.join(out_dir, ADDRESS_FORMAT_DATA_FILENAME), 'w')
        writer = csv.writer(formatted_file, 'tsv_no_quote')

    remove_keys = OSM_IGNORE_KEYS

    for key, value in parse_osm(infile):
        try:
            latitude, longitude = latlon_to_floats(value['lat'], value['lon'])
        except Exception:
            continue

        country, candidate_languages, language_props = country_and_languages(language_rtree, latitude, longitude)
        if not (country and candidate_languages):
            continue

        for key in remove_keys:
            _ = value.pop(key, None)

        language = None
        if tag_components:
            if len(candidate_languages) == 1:
                language = candidate_languages[0]['lang']
            else:
                street = value.get('addr:street', None)
                if street is None:
                    continue
                language = disambiguate_language(street, [(l['lang'], l['default']) for l in candidate_languages])

        formatted_address = formatter.format_address(country, value, tag_components=tag_components)
        if formatted_address is not None:
            formatted_address = tsv_string(formatted_address)
            if not formatted_address or not formatted_address.strip():
                continue
            if tag_components:
                row = (language, country, formatted_address)
            else:
                row = (formatted_address,)

            writer.writerow(row)

        if formatted_address is not None:
            i += 1
            if i % 1000 == 0 and i > 0:
                print 'did', i, 'formatted addresses'


NAME_KEYS = (
    'name',
    'addr:housename',
)
COUNTRY_KEYS = (
    'country',
    'country_name',
    'addr:country',
)
POSTAL_KEYS = (
    'postcode',
    'postal_code',
    'addr:postcode',
    'addr:postal_code',
)


def build_address_format_training_data_limited(language_rtree, infile, out_dir):
    '''
    Creates a special kind of formatted address training data from OSM's addr:* tags
    but are designed for use in language classification. These records are similar 
    to the untagged formatted records but include the language and country
    (suitable for concatenation with the rest of the language training data),
    and remove several fields like country which usually do not contain helpful
    information for classifying the language.

    Example:

    nb      no      Olaf Ryes Plass 8 | Oslo
    '''
    i = 0

    formatter = AddressFormatter()

    f = open(os.path.join(out_dir, ADDRESS_FORMAT_DATA_LANGUAGE_FILENAME), 'w')
    writer = csv.writer(f, 'tsv_no_quote')

    remove_keys = NAME_KEYS + COUNTRY_KEYS + POSTAL_KEYS + OSM_IGNORE_KEYS

    for key, value in parse_osm(infile):
        try:
            latitude, longitude = latlon_to_floats(value['lat'], value['lon'])
        except Exception:
            continue

        for k in remove_keys:
            _ = value.pop(k, None)

        if not value:
            continue

        country, name_language = get_language_names(language_rtree, key, value, tag_prefix='addr:street')
        if not name_language:
            continue

        single_language = len(name_language) == 1
        for lang, val in name_language.iteritems():
            if lang not in languages:
                continue

            address_dict = value.copy()
            for k in address_dict.keys():
                namespaced_val = u'{}:{}'.format(k, lang)
                if namespaced_val in address_dict:
                    address_dict[k] = address_dict[namespaced_val]
                elif not single_language:
                    address_dict.pop(k)

            if not address_dict:
                continue

            formatted_address_untagged = formatter.format_address(country, address_dict, tag_components=False)
            if formatted_address_untagged is not None:
                formatted_address_untagged = tsv_string(formatted_address_untagged)

                writer.writerow((lang, country, formatted_address_untagged))

        i += 1
        if i % 1000 == 0 and i > 0:
            print 'did', i, 'formatted addresses'


apposition_regex = re.compile('(.*[^\s])[\s]*\([\s]*(.*[^\s])[\s]*\)$', re.I)

html_parser = HTMLParser.HTMLParser()


def normalize_wikipedia_title(title):
    match = apposition_regex.match(title)
    if match:
        title = match.group(1)

    title = safe_decode(title)
    title = html_parser.unescape(title)
    title = urllib.unquote_plus(title)

    return title.replace(u'_', u' ').strip()


def build_toponym_training_data(language_rtree, infile, out_dir):
    '''
    Data set of toponyms by language and country which should assist
    in language classification. OSM tends to use the native language
    by default (e.g. Москва instead of Moscow). Toponyms get messy
    due to factors like colonialism, historical names, name borrowing
    and the shortness of the names generally. In these cases
    we're more strict as to what constitutes a valid language for a
    given country.

    Example:
    ja      jp      東京都
    '''
    i = 0
    f = open(os.path.join(out_dir, TOPONYM_LANGUAGE_DATA_FILENAME), 'w')
    writer = csv.writer(f, 'tsv_no_quote')

    for key, value in parse_osm(infile):
        if not sum((1 for k, v in value.iteritems() if k.startswith('name'))) > 0:
            continue

        try:
            latitude, longitude = latlon_to_floats(value['lat'], value['lon'])
        except Exception:
            continue

        country, candidate_languages, language_props = country_and_languages(language_rtree, latitude, longitude)
        if not (country and candidate_languages):
            continue

        name_language = defaultdict(list)

        official = official_languages[country]

        default_langs = set([l for l, default in official.iteritems() if default])

        regional_langs = list(chain(*(p['languages'] for p in language_props if p.get('admin_level', 0) > 0)))

        top_lang = None
        if len(official) > 0:
            top_lang = official.iterkeys().next()

        # E.g. Hindi in India, Urdu in Pakistan
        if top_lang is not None and top_lang not in WELL_REPRESENTED_LANGUAGES and len(default_langs) > 1:
            default_langs -= WELL_REPRESENTED_LANGUAGES

        valid_languages = set([l['lang'] for l in candidate_languages])

        '''
        WELL_REPRESENTED_LANGUAGES are languages like English, French, etc. for which we have a lot of data
        WELL_REPRESENTED_LANGUAGE_COUNTRIES are more-or-less the "origin" countries for said languages where
        we can take the place names as examples of the language itself (e.g. place names in France are examples
        of French, whereas place names in much of Francophone Africa tend to get their names from languages
        other than French, even though French is the official language.
        '''
        valid_languages -= set([lang for lang in valid_languages if lang in WELL_REPRESENTED_LANGUAGES and country not in WELL_REPRESENTED_LANGUAGE_COUNTRIES[lang]])

        valid_languages |= default_langs

        if not valid_languages:
            continue

        have_qualified_names = False

        for k, v in value.iteritems():
            if not k.startswith('name:'):
                continue

            norm = normalize_osm_name_tag(k)
            norm_sans_script = normalize_osm_name_tag(k, script=True)

            if norm in languages:
                lang = norm
            elif norm_sans_script in languages:
                lang = norm_sans_script
            else:
                continue

            if lang in valid_languages:
                have_qualified_names = True
                name_language[lang].append(v)

        if not have_qualified_names and len(regional_langs) <= 1 and 'name' in value and len(valid_languages) == 1:
            name_language[top_lang].append(value['name'])

        for k, v in name_language.iteritems():
            for s in v:
                s = s.strip()
                if not s:
                    continue
                writer.writerow((k, country, tsv_string(s)))
            if i % 1000 == 0 and i > 0:
                print 'did', i, 'toponyms'
            i += 1

    f.close()


def build_address_training_data(langauge_rtree, infile, out_dir, format=False):
    '''
    Creates training set similar to the ways data but using addr:street tags instead.
    These may be slightly closer to what we'd see in real live addresses, containing
    variations, some abbreviations (although this is discouraged in OSM), etc.

    Example record:
    eu      es      Errebal kalea
    '''
    i = 0
    f = open(os.path.join(out_dir, ADDRESS_LANGUAGE_DATA_FILENAME), 'w')
    writer = csv.writer(f, 'tsv_no_quote')

    for key, value in parse_osm(infile):
        country, street_language = get_language_names(language_rtree, key, value, tag_prefix='addr:street')
        if not street_language:
            continue

        for k, v in street_language.iteritems():
            for s in v:
                s = s.strip()
                if not s:
                    continue
                if k in languages:
                    writer.writerow((k, country, tsv_string(s)))
            if i % 1000 == 0 and i > 0:
                print 'did', i, 'streets'
            i += 1

    f.close()

VENUE_LANGUAGE_DATA_FILENAME = 'names_by_language.tsv'


def build_venue_training_data(language_rtree, infile, out_dir):
    i = 0

    f = open(os.path.join(out_dir, VENUE_LANGUAGE_DATA_FILENAME), 'w')
    writer = csv.writer(f, 'tsv_no_quote')

    for key, value in parse_osm(infile):
        country, name_language = get_language_names(language_rtree, key, value, tag_prefix='name')
        if not name_language:
            continue

        venue_type = None
        for key in (u'amenity', u'building'):
            amenity = value.get(key, u'').strip()
            if amenity in ('yes', 'y'):
                continue

            if amenity:
                venue_type = u':'.join([key, amenity])
                break

        if venue_type is None:
            continue

        for k, v in name_language.iteritems():
            for s in v:
                s = s.strip()
                if k in languages:
                    writer.writerow((k, country, safe_encode(venue_type), tsv_string(s)))
            if i % 1000 == 0 and i > 0:
                print 'did', i, 'venues'
            i += 1

    f.close()

if __name__ == '__main__':
    # Handle argument parsing here
    parser = argparse.ArgumentParser()

    parser.add_argument('-s', '--streets-file',
                        help='Path to planet-ways.osm')

    parser.add_argument('-a', '--address-file',
                        help='Path to planet-addresses.osm')

    parser.add_argument('-v', '--venues-file',
                        help='Path to planet-venues.osm')

    parser.add_argument('-b', '--borders-file',
                        help='Path to planet-borders.osm')

    parser.add_argument('-f', '--format-only',
                        action='store_true',
                        default=False,
                        help='Save formatted addresses (slow)')

    parser.add_argument('-u', '--untagged',
                        action='store_true',
                        default=False,
                        help='Save untagged formatted addresses (slow)')

    parser.add_argument('-l', '--limited-addresses',
                        action='store_true',
                        default=False,
                        help='Save formatted addresses without house names or country (slow)')

    parser.add_argument('-t', '--temp-dir',
                        default=tempfile.gettempdir(),
                        help='Temp directory to use')

    parser.add_argument('-r', '--rtree-dir',
                        required=True,
                        help='Language RTree directory')

    parser.add_argument('-o', '--out-dir',
                        default=os.getcwd(),
                        help='Output directory')

    args = parser.parse_args()

    init_languages()

    language_rtree = LanguagePolygonIndex.load(args.rtree_dir)

    street_types_gazetteer.configure()

    # Can parallelize
    if args.streets_file:
        build_ways_training_data(language_rtree, args.streets_file, args.out_dir)
    if args.borders_file:
        build_toponym_training_data(language_rtree, args.borders_file, args.out_dir)
    if args.address_file and not args.format_only and not args.limited_addresses:
        build_address_training_data(language_rtree, args.address_file, args.out_dir)
    if args.address_file and args.format_only:
        build_address_format_training_data(language_rtree, args.address_file, args.out_dir, tag_components=not args.untagged)
    if args.address_file and args.limited_addresses:
        build_address_format_training_data_limited(language_rtree, args.address_file, args.out_dir)
    if args.venues_file:
        build_venue_training_data(language_rtree, args.venues_file, args.out_dir)
