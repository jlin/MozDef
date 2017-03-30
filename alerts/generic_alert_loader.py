#!/usr/bin/env python

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Copyright (c) 2017 Mozilla Corporation
#
# Contributors:
# kang@mozilla.com
# bmyers@mozilla.com

# TODO: Dont use query_models, nicer fixes for AlertTask

from lib.alerttask import AlertTask
from query_models import SearchQuery, TermMatch, QueryStringMatch
import hjson
import logging
import sys
import traceback
import glob
import os
from configlib import getConfig, OptionParser

# Minimum data needed for an alert (this is an example alert json)
'''
    {
        # Lucene search string
        'search_string': 'field1: matchingvalue and field2: matchingothervalue',

        # ES Filters as such: [['field', 'value'], ['field', 'value']]
        'filters': [],

        # What to aggregate on if we get multiple matches?
        'aggregation_key': 'summary',

        # How long to search and aggregate for? The longer the slower.
        # These defaults work well for alerts that basically don't *need*
        # much aggregation
        'threshold': {
            'timerange_min': 5,
            'count': 1
        },

        # This is the category that will show up in mozdef, and the severity
        'alert_category': 'generic_alerts',
        'alert_severity': 'INFO',

        # This will show up as the alert text when it trigger
        'summary': 'Example summary that shows up in the alert',

        # This helps sorting out alerts, so it's nice if you fill this in
        'tags': ['generic'],

        # This is the alert documentation
        'url': 'https://mozilla.org'
    }
'''


logger = logging.getLogger()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')


class DotDict(dict):
    '''dict.item notation for dict()'s'''
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__

    def __init__(self, dct):
        for key, value in dct.items():
            if hasattr(value, 'keys'):
                value = DotDict(value)
            self[key] = value


class AlertGenericLoader(AlertTask):
    required_fields = [
        "search_string",
        "filters",
        "threshold",
        "aggregation_key",
        "alert_category",
        "tags",
        "alert_severity",
        "summary",
        "url",
    ]

    def validate_alert(self, alert):
        for key in self.required_fields:
            if key not in alert:
                logger.error('Your alert does not have the required field {}'.format(key))
                raise KeyError

    def load_configs(self):
        '''Load all configured rules'''
        self.configs = []
        rules_location = os.path.join(self.config.alert_data_location, "rules")
        files = glob.glob(rules_location + "/*.json")
        for f in files:
            with open(f) as fd:
                try:
                    cfg = DotDict(hjson.load(fd))
                    self.validate_alert(cfg)
                    self.configs.append(cfg)
                except Exception:
                    logger.error("Loading rule file {} failed".format(f))

    def initConfiguration(self):
        myparser = OptionParser()
        (self.config, args) = myparser.parse_args([])
        self.config.alert_data_location = getConfig('alert_data_location', '', self.config_file)

    def process_alert(self, config):
        search_query = SearchQuery(minutes=int(config.threshold.timerange_min))
        terms = []
        for i in config.filters:
            terms.append(TermMatch(i[0], i[1]))
        terms.append(QueryStringMatch(str(config.search_string)))
        search_query.add_must(terms)
        self.filtersManual(search_query)
        self.searchEventsAggregated(config.aggregation_key, samplesLimit=int(config.threshold.count))
        self.walkAggregations(threshold=int(config.threshold.count), config=config)

    def main(self):
        self.config_file = './generic_alert_loader.conf'
        self.initConfiguration()

        self.load_configs()
        for cfg in self.configs:
            try:
                self.process_alert(cfg)
            except Exception:
                traceback.print_exc(file=sys.stdout)
                logger.error("Processing rule file {} failed".format(cfg.__str__()))

    def onAggregation(self, aggreg):
        # aggreg['count']: number of items in the aggregation, ex: number of failed login attempts
        # aggreg['value']: value of the aggregation field, ex: toto@example.com
        # aggreg['events']: list of events in the aggregation
        category = aggreg['config']['alert_category']
        tags = aggreg['config']['tags']
        severity = aggreg['config']['alert_severity']
        url = aggreg['config']['url']

        # Find all affected hosts
        # Normally, the hostname data is in e.details.hostname so try that first,
        # but fall back to e.hostname if it is missing, or nothing at all if there's no hostname! ;-)
        hostnames = []
        for e in aggreg['events']:
            event_source = e['_source']
            if 'details' in event_source and 'hostname' in event_source['details']:
                hostnames.append(event_source['details']['hostname'])
            elif 'hostname' in event_source:
                hostnames.append(event_source['hostname'])

        summary = '{} ({}): {}'.format(
            aggreg['config']['summary'],
            aggreg['count'],
            aggreg['value'],
        )

        if hostnames:
            summary += ' [{}]'.format(','.join(hostnames))

        return self.createAlertDict(summary, category, tags, aggreg['events'], severity, url)
