import argparse
import os
import requests
import six
import subprocess
import sys
import tempfile
import yaml

from six.moves.urllib_parse import urljoin

this_dir = os.path.realpath(os.path.dirname(__file__))
sys.path.append(os.path.realpath(os.path.join(os.pardir, os.pardir)))

from geodata.openaddresses.config import openaddresses_config
from geodata.csv_utils import unicode_csv_reader
from geodata.file_utils import ensure_dir, download_file, cd, remove_file

BASE_OPENADDRESSES_DATA_URL = 'http://results.openaddresses.io'

OPENADDRESSES_LATEST_DIR = urljoin(BASE_OPENADDRESSES_DATA_URL, 'latest/run/')

OPENADDRESSES_STATE_FILE_NAME = 'state.txt'
OPENADDRESSES_STATE_URL = urljoin(BASE_OPENADDRESSES_DATA_URL, OPENADDRESSES_STATE_FILE_NAME)


def download_and_unzip_file(url, out_dir):
    zip_filename = url.rsplit('/', 1)[-1].strip()
    zip_local_path = os.path.join(out_dir, zip_filename)

    success = download_file(url, zip_local_path) and subprocess.check_call(['unzip', '-o', zip_local_path, '-d', out_dir]) == 0

    if os.path.exists(zip_local_path):
        remove_file(zip_local_path)

    return success


def openaddresses_download_all_files(out_dir):
    temp_dir = tempfile.gettempdir()

    local_state_file_path = os.path.join(temp_dir, OPENADDRESSES_STATE_FILE_NAME)
    if not download_file(OPENADDRESSES_STATE_URL, local_state_file_path):
        sys.exit('Could not download state.txt file')

    reader = unicode_csv_reader(open(local_state_file_path), delimiter='\t')
    headers = reader.next()

    source_index = headers.index('source')
    url_index = headers.index('processed')

    for row in reader:
        source = row[source_index].rsplit('.')[0]
        processed = row[url_index]
        if not processed or not processed.strip():
            continue

        print(six.u('doing {}').format(source))
        success = download_and_unzip_file(processed, out_dir)
        if not success:
            print(six.u('ERR: could not download {}').format(source))

    remove_file(local_state_file_path)


def openaddresses_download_configured_files(out_dir):
    for path in openaddresses_config.sources:
        source = '/'.join(path)
        zip_path = source + '.zip'
        url = urljoin(OPENADDRESSES_LATEST_DIR, zip_path)

        print(six.u('doing {}').format(source))
        success = download_and_unzip_file(url, out_dir)
        if not success:
            print(six.u('ERR: could not download {}').format(source))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('-o', '--out-dir',
                        required=True,
                        help='Output directory')

    parser.add_argument('--all', action='store_true',
                        default=False, help='Download all completed OpenAddresses files')

    args = parser.parse_args()
    ensure_dir(args.out_dir)

    if args.all:
        openaddresses_download_all_files(args.out_dir)
    else:
        openaddresses_download_configured_files(args.out_dir)
