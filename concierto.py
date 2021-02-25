
import argparse
import logging
import os
import re
import sys
from time import time
import traceback

import json

from ansible.module_utils.six import iteritems
from ansible.module_utils.six.moves import configparser as ConfigParser
from ansible.module_utils.six.moves.urllib.parse import urlencode

from ansible.module_utils.urls import open_url


class ConciertoDefaults(object):
    ASSETS_API_ENDPOINT = '%s/api/v1/listAwsHost'
    SPECIAL_ATTRIBUTES = set([
        'CREATED',
        'DELETED',
        'UPDATED',
        'STATE',
    ])
    LOG_FORMAT = '%(asctime)-15s %(message)s'


class Error(Exception):
    pass


class MaxRetriesError(Error):
    pass


class ConciertoInventory(object):

    def __init__(self):
        """ Constructs ConciertoInventory object and reads all configuration. """

        self.inventory = dict()  # A list of groups and the hosts in that group
        self.cache = dict()  # Details about hosts in the inventory

        # Read settings and parse CLI arguments
        self.read_settings()
        #self.parse_cli_args()

        logging.basicConfig(format=ConciertoDefaults.LOG_FORMAT,
                            filename=self.log_location)
        self.log = logging.getLogger('ConciertoInventory')

    def _asset_get_attribute(self, asset, attrib):
        """ Returns a user-defined attribute from an asset if it exists; otherwise,
            returns None. """

        if 'ATTRIBS' in asset:
            for attrib_block in asset['ATTRIBS'].keys():
                if attrib in asset['ATTRIBS'][attrib_block]:
                    return asset['ATTRIBS'][attrib_block][attrib]
        return None

    def _asset_has_attribute(self, asset, attrib):
        """ Returns whether a user-defined attribute is present on an asset. """

        if 'ATTRIBS' in asset:
            for attrib_block in asset['ATTRIBS'].keys():
                if attrib in asset['ATTRIBS'][attrib_block]:
                    return True
        return False

    def run(self):
        """ Main execution path """

        # Updates cache if cache is not present or has expired.
        successful = True
        if self.args.refresh_cache:
            successful = self.update_cache()
        elif not self.is_cache_valid():
            successful = self.update_cache()
        else:
            successful = self.load_inventory_from_cache()
            successful &= self.load_cache_from_cache()

        data_to_print = ""

        # Data to print
        if self.args.host:
            data_to_print = self.get_host_info()

        elif self.args.list:
            # Display list of instances for inventory
            data_to_print = self.json_format_dict(self.inventory, self.args.pretty)

        else:  # default action with no options
            data_to_print = self.json_format_dict(self.inventory, self.args.pretty)

        print(data_to_print)
        return successful

    def find_assets(self, attributes=None, operation='AND'):
        """ Obtains concierto assets matching the provided attributes. """

        attributes = {} if attributes is None else attributes

        # Formats asset search query to locate assets matching attributes, using
        # the CQL search feature as described here:
        # http://tumblr.github.io/collins/recipes.html
        attributes_query = ['='.join(attr_pair) for attr_pair in iteritems(attributes)]
        query_parameters = {
            'details': ['True'],
            'operation': [operation],
            'query': attributes_query,
            'remoteLookup': [str(self.query_remote_dcs)],
            'size': [self.results_per_query],
            'type': [self.concierto_asset_type],
        }
        assets = []
        cur_page = 0
        num_retries = 0
        # Locates all assets matching the provided query, exhausting pagination.
        while True:
            if num_retries == self.concierto_max_retries:
                raise MaxRetriesError("Maximum of %s retries reached; giving up" % self.concierto_max_retries)
            query_parameters['page'] = cur_page
            query_url = "%s?%s" % (
                (ConciertoDefaults.ASSETS_API_ENDPOINT % self.concierto_host),
                urlencode(query_parameters, doseq=True)
            )
            try:
                response = open_url(query_url,
                                    timeout=self.concierto_timeout_secs,
                                    url_username=self.concierto_username,
                                    url_password=self.concierto_password,
                                    force_basic_auth=True)
                json_response = json.loads(response.read())
                # Adds any assets found to the array of assets.
                assets += json_response['data']['Data']
                # If we've retrieved all of our assets, breaks out of the loop.
                if len(json_response['data']['Data']) == 0:
                    break
                cur_page += 1
                num_retries = 0
            except Exception:
                self.log.error("Error while communicating with concierto, retrying:\n%s", traceback.format_exc())
                num_retries += 1
        return assets

    def is_cache_valid(self):
        """ Determines if the cache files have expired, or if it is still valid """

        if os.path.isfile(self.cache_path_cache):
            mod_time = os.path.getmtime(self.cache_path_cache)
            current_time = time()
            if (mod_time + self.cache_max_age) > current_time:
                if os.path.isfile(self.cache_path_inventory):
                    return True

        return False

    def read_settings(self):
        """ Reads the settings from the concierto.ini file """

        config_loc = os.getenv('CONCIERTO_CONFIG', os.path.dirname(os.path.realpath(__file__)) + '/concierto.ini')

        config = ConfigParser.SafeConfigParser()
        config.read(os.path.dirname(os.path.realpath(__file__)) + '/concierto.ini')

        self.concierto_host = config.get('concierto', 'host')
        self.concierto_clientid=os.getenv('CONCIERTO_CLIENTID',config.get('concierto','clientid'))
        self.concierto_username = os.getenv('CONCIERTO_USERNAME', config.get('concierto', 'username'))
        self.concierto_password = os.getenv('CONCIERTO_PASSWORD', config.get('concierto', 'password'))
        self.concierto_asset_type = os.getenv('CONCIERTO_ASSET_TYPE', config.get('concierto', 'asset_type'))
        self.concierto_timeout_secs = config.getint('CONCIERTO', 'timeout_secs')
        self.concierto_max_retries = config.getint('CONCIERTO', 'max_retries')

        self.results_per_query = config.getint('concierto', 'results_per_query')
        self.ip_address_index = config.getint('concierto', 'ip_address_index')
        self.query_remote_dcs = config.getboolean('concierto', 'query_remote_dcs')
        self.prefer_hostnames = config.getboolean('concierto', 'prefer_hostnames')
_
        cache_path = config.get('concierto', 'cache_path')
        self.cache_path_cache = cache_path + \
            '/ansible-concierto-%s.cache' % self.concierto_asset_type
        self.cache_path_inventory = cache_path + \
            '/ansible-concierto-%s.index' % self.concierto_asset_type
        self.cache_max_age = config.getint('concierto', 'cache_max_age')

        log_path = config.get('concierto', 'log_path')
        self.log_location = log_path + '/ansible-concierto.log'

    def update_cache(self):
        """ Make calls to concierto and saves the output in a cache """

        self.cache = dict()
        self.inventory = dict()

        # Locates all server assets from Concierto.
        try:
            server_assets = self.find_assets()
        except Exception:
            self.log.error("Error while locating assets from concierto:\n%s", traceback.format_exc())
            return False

        for asset in server_assets:
            # Determines the index to retrieve the asset's IP address either by an
            # attribute set on the Collins asset or the pre-configured value.
            if self._asset_has_attribute(asset, 'ANSIBLE_IP_INDEX'):
                ip_index = self._asset_get_attribute(asset, 'ANSIBLE_IP_INDEX')
                try:
                    ip_index = int(ip_index)
                except Exception:
                    self.log.error(
                        "ANSIBLE_IP_INDEX attribute on asset %s not an integer: %s", asset,
                        ip_index)
            else:
                ip_index = self.ip_address_index

            asset['COLLINS'] = {}

            # Attempts to locate the asset's primary identifier (hostname or IP address),
            # which will be used to index the asset throughout the Ansible inventory.
            if self.prefer_hostnames and self._asset_has_attribute(asset, 'HOSTNAME'):
                asset_identifier = self._asset_get_attribute(asset, 'HOSTNAME')
            elif 'ADDRESSES' not in asset:
                self.log.warning("No IP addresses found for asset '%s', skipping", asset)
                continue
            elif len(asset['ADDRESSES']) < ip_index + 1:
                self.log.warning(
                    "No IP address found at index %s for asset '%s', skipping",
                    ip_index, asset)
                continue
            else:
                asset_identifier = asset['ADDRESSES'][ip_index]['ADDRESS']

            # Adds an asset index to the Ansible inventory based upon unpacking
            # the name of the asset's current STATE from its dictionary.
            if 'STATE' in asset['ASSET'] and asset['ASSET']['STATE']:
                state_inventory_key = self.to_safe(
                    'STATE-%s' % asset['ASSET']['STATE']['NAME'])
                self.push(self.inventory, state_inventory_key, asset_identifier)

            # Indexes asset by all user-defined concierto attributes.
            if 'ATTRIBS' in asset:
                for attrib_block in asset['ATTRIBS'].keys():
                    for attrib in asset['ATTRIBS'][attrib_block].keys():
                        asset['COLLINS'][attrib] = asset['ATTRIBS'][attrib_block][attrib]
                        attrib_key = self.to_safe('%s-%s' % (attrib, asset['ATTRIBS'][attrib_block][attrib]))
                        self.push(self.inventory, attrib_key, asset_identifier)

            # Indexes asset by all built-in concierto attributes.
            for attribute in asset['ASSET'].keys():
                if attribute not in ConciertoDefaults.SPECIAL_ATTRIBUTES:
                    attribute_val = asset['ASSET'][attribute]
                    if attribute_val is not None:
                        attrib_key = self.to_safe('%s-%s' % (attribute, attribute_val))
                        self.push(self.inventory, attrib_key, asset_identifier)

            # Indexes asset by hardware product information.
            if 'HARDWARE' in asset:
                if 'PRODUCT' in asset['HARDWARE']['BASE']:
                    product = asset['HARDWARE']['BASE']['PRODUCT']
                    if product:
                        product_key = self.to_safe(
                            'HARDWARE-PRODUCT-%s' % asset['HARDWARE']['BASE']['PRODUCT'])
                        self.push(self.inventory, product_key, asset_identifier)

            # Indexing now complete, adds the host details to the asset cache.
            self.cache[asset_identifier] = asset

        try:
            self.write_to_cache(self.cache, self.cache_path_cache)
            self.write_to_cache(self.inventory, self.cache_path_inventory)
        except Exception:
            self.log.error("Error while writing to cache:\n%s", traceback.format_exc())
            return False
        return True

    def push(self, dictionary, key, value):
        """ Adds a value to a list at a dictionary key, creating the list if it doesn't
            exist. """

        if key not in dictionary:
            dictionary[key] = []
        dictionary[key].append(value)

    def get_host_info(self):
        """ Get variables about a specific host. """

        if not self.cache or len(self.cache) == 0:
            # Need to load index from cache
            self.load_cache_from_cache()

        if self.args.host not in self.cache:
            # try updating the cache
            self.update_cache()

            if self.args.host not in self.cache:
                # host might not exist anymore
                return self.json_format_dict({}, self.args.pretty)

        return self.json_format_dict(self.cache[self.args.host], self.args.pretty)

    def load_inventory_from_cache(self):
        """ Reads the index from the cache file sets self.index """

        try:
            cache = open(self.cache_path_inventory, 'r')
            json_inventory = cache.read()
            self.inventory = json.loads(json_inventory)
            return True
        except Exception:
            self.log.error("Error while loading inventory:\n%s",
                           traceback.format_exc())
            self.inventory = {}
            return False

    def load_cache_from_cache(self):
        """ Reads the cache from the cache file sets self.cache """

        try:
            cache = open(self.cache_path_cache, 'r')
            json_cache = cache.read()
            self.cache = json.loads(json_cache)
            return True
        except Exception:
            self.log.error("Error while loading host cache:\n%s",
                           traceback.format_exc())
            self.cache = {}
            return False

    def write_to_cache(self, data, filename):
        """ Writes data in JSON format to a specified file. """

        json_data = self.json_format_dict(data, self.args.pretty)
        cache = open(filename, 'w')
        cache.write(json_data)
        cache.close()

    def to_safe(self, word):
        """ Converts 'bad' characters in a string to underscores so they
            can be used as Ansible groups """

        return re.sub(r"[^A-Za-z0-9\-]", "_", word)

    def json_format_dict(self, data, pretty=False):
        """ Converts a dict to a JSON object and dumps it as a formatted string """

        if pretty:
            return json.dumps(data, sort_keys=True, indent=2)
        else:
            return json.dumps(data)


if __name__ in '__main__':
    inventory = ConciertoInventory()
    if inventory.run():
        sys.exit(0)
    else:
        sys.exit(-1)