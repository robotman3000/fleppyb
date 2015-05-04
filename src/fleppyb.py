#!/usr/bin/python
# vi:si:et:sw=4:sts=4:ts=4
# -*- coding: UTF-8 -*-
# -*- Mode: Python -*-
#
# Copyright (C) 2011 Bertera Pietro <pietro@bertera.it>

# This file may be distributed and/or modified under the terms of
# the GNU General Public License version 2 as published by
# the Free Software Foundation.
# This file is distributed without any warranty; without even the implied
# warranty of merchantability or fitness for a particular purpose.
#
# Fleppy Backend
# Flexible Powerdns PYthon Backend

import sys
import logging
import os
import re
import time
import random

import dns.resolver
import socket

from IPy import IP
from ConfigParser import RawConfigParser
from pdns import PowerDNSBackend, DNSAnswer, DNSQuery

DEBUG=True
CONFIG_FILE="/etc/powerdns/fleppyb/fleppyb.ini"
PARSE_CONFIG_ONCE=False
LOGFILE="/var/log/fleppyb.log"

class STATICDNSBackend(DNSAnswer):

    def __init__(self, options, logger):
        self.options = options
        self.logger = logger
    
    def query(self, query):
        answer = []
        static_options = {}

        for k,v in self.options:
            static_options[k] = v

        if "delay" in static_options:

            if static_options["delay"].split(":")[0] == "random":
                try:
                    min = float(static_options["delay"].split(":")[1])
                    max = float(static_options["delay"].split(":")[2])
                except IndexError, e:
                    self.logger.error("[STATIC] random delay must be in this form: random:min:max")
                    raise e

                delay = random.uniform(min, max)
            else:
                delay = float(static_options["delay"])
            self.logger.debug("[STATIC] waiting %f" % delay)
            time.sleep(delay)
        
        if "answer" not in static_options:
            self.logger.debug("[STATIC] no answer defined")
            return answer

      	for tmp_answer in static_options["answer"].split(","):
            try:
                answer_type = tmp_answer.split(":")[0]
                answer_ttl = int(tmp_answer.split(":")[1])
                answer_value = tmp_answer.split(":")[2]
                answer.append(DNSAnswer(query.qname, "IN",
                            answer_type, answer_ttl, 1, answer_value))
            except Exception, e:
                self.logger.error("[STATIC] error parsing %s answer" % tmp_answer)
                self.logger.error("[STATIC] answer must be in this format: TYPE:TTL:VALUE,TYPE:TTL:VALUE")
                self.logger.error("[STATIC] Eg. A:9932:10.102.3.1,A:9932:10.102.3.2")
                raise e

        return answer

class LDAPDNSBackend(DNSAnswer):
    # TODO: make logger object external

    def __init__(self, options, logger):
        self.options = options
        self.logger = logger
        
    def query(self, query):
        import ldap
        from ldap.cidict import cidict
        ldap_options = cidict()
        attribute_map = cidict()
        query_map = cidict()
        answer = []

        for k, v in self.options:
            ldap_options[k] = v
            if k.endswith('_attribute'):
                # dict with qery type in keys end ldap attribute in value
                attribute_map[k.split('_attribute')[0]] = v
                # vice-versa
                query_map[v] = k.split('_attribute')[0]

        if "ttl_default" not in ldap_options:
            self.logger.error("[LDAP] ttl_default not defined in ldap attributes mapping")
            sys.exit(-1)

        if "cname_attribute" not in ldap_options:
            self.logger.error("[LDAP] cname_attribute not defined in ldap attributes mapping")
            sys.exit(-1)
            
        try:
            ldap_conn = ldap.initialize(ldap_options['ldap_uri'])
            if ldap_options.has_key("bind") and (ldap_options['bind'] ==
                    "True" or ldap_options['bind'] == "1"):
                self.logger.debug("[LDAP] trying to bind to ldap server using credentials")
                ldap_conn.simple_bind_s(ldap_options['bind_dn'],
                        ldap_options['bind_password'])
                self.logger.debug("[LDAP] bind successful")
            else:
                self.logger.debug("[LDAP] accessing to ldap server anonymously")
            # TODO:
            if 'dot_in_dn' in ldap_options:
                dn_token = ",%s=" % ldap_options['dot_in_dn']
                q  = ".%s" % query.qname
                qname_dn = dn_token.join(q.split(".")).lstrip(',')
            else:
                qname_dn = "UNDEFINED"

            ldap_base = ldap_options['base'] % ({'qname': query.qname,
                'qtype': query.qtype, 'remote_ip': query.remote_ip,
                'local_ip': query.local_ip, 'qname_dn': qname_dn})
            self.logger.debug("formatted base: %s" % ldap_base)
            
            ldap_query = ldap_options['query'] % ({
                'qname': query.qname,
                'qtype': query.qtype,
                'remote_ip': query.remote_ip,
                'local_ip': query.local_ip, 
                'rqname': query.rqname
                })
            ldap_query = ldap_query.replace("*", "\*")
            self.logger.debug("[LDAP] formatted query: %s" % ldap_query)
           
            if query.qtype.lower() == "any":
                attributes = query_map.keys()
            else:
                if query.qtype.lower() not in attribute_map.keys():
                    self.logger.warning("[LDAP] attribute for query %s not found in map" % query.qtype)
                    return answer
                attributes = [attribute_map[query.qtype]]

            attributes.append(ldap_options['ttl_attribute'])
            attributes.append(ldap_options['cname_attribute'])
            self.logger.debug("[LDAP] attributes: %s" % attributes)
            
            #TODO: scope in config file
            try:
                res = ldap_conn.search_s(ldap_base, ldap.SCOPE_SUBTREE ,
                    ldap_query, attributes)
            except Exception, e:
                self.logger.info("[LDAP] ldap query error: %s", e)
                return answer

            for entry in res:
                # search for ttl attribute in LDAP otherwise use default_ttl
                if ldap_options['ttl_attribute'] not in entry[1].keys():
                    self.logger.debug("[LDAP] ttl entry (%s) not found, using default: %s" %
                            (ldap_options['ttl_attribute'],
                                ldap_options['ttl_default']))
                    ttl = int(ldap_options['ttl_default'])
                else:
                    ttl = int(entry[1][ldap_options['ttl_attribute']][0])
                
                # compose the answer 
                for attribute in entry[1].keys():
                    for att in entry[1][attribute]:
                        self.logger.debug("[LDAP] found data in database: %s" % att)
                        answer.append(DNSAnswer(query.qname, "IN",
                            query_map[attribute].upper(), ttl, 1, att))

        except Exception, e:
            self.logger.exception("[LDAP] Exception: %s" %e)
        
        return answer

class RecurseBackend(DNSAnswer):

    def __init__(self, logger):
        self.logger = logger

    def query(self, query):
        answer = []
        useOpenDns = False

        googleResolver = dns.resolver.Resolver()
        googleResolver.nameservers = ['8.8.8.8', '4.4.4.4']

        openDNSResolver = dns.resolver.Resolver()
        openDNSResolver.nameservers = ['208.67.222.222']

        if query.remote_ip == '192.168.8.118':
            useOpenDns = True

        if useOpenDns:
            # Remove the 'A' to not get only A records
            dnsAnswer = openDNSResolver.query(query.qname, 'A')
        else:
            dnsAnswer = googleResolver.query(query.qname, 'A')

        address = '127.0.0.2'
        for a in dnsAnswer:
            self.logger.debug("Answer rdclass " + str(a.rdclass) + " Answer rdtype " + str(a.rdtype))
            self.logger.debug(a)
            self.logger.debug(dnsAnswer)
            # this if statement is broken and not yet finished
            # dont use it
            if a.rdclass == 'IN':
                if a.rdtype == 'A':
                    address = a.address
                elif a.rdtype == 'AAAA':
                    address = a.address
                elif a.rdtype == 'SOA':
                    address = '127.0.0.1'

            answer.append(DNSAnswer(query.qname, query.qtype, query.qclass, 3600, 1, a.address))

        return answer

class RobotmanBackend(object):

    def __init__(self, logger):
        self.logger = logger

    def query(self, q):
        answer = []
        backend = RecurseBackend(self.logger)
        answer = backend.query(q)
        return answer

    # TODO: AXFR not implemented !
    def axfr(self, a=None, b=None, c=None):
        self.logger.warning("AXFR query not implemented")
        return []

class FleppyBackend(object):

    def _keyfunc(self, string):
        return int(string.split(':')[0])

    def __init__(self, logger):
        self.logger = logger

        if PARSE_CONFIG_ONCE:
            self.parse_conf()

    def parse_conf(self):
        self.cfg =  RawConfigParser()
        self.cfg.read(CONFIG_FILE)
        self.logger.debug("reloading configuration")
        for section_name in self.cfg.sections():
            self.logger.debug ('Section: [%s]' % section_name)
            self.logger.debug('  Options: %s' % self.cfg.options(section_name))
            for name, value in self.cfg.items(section_name):
                self.logger.debug( '  %s = %s' % (name, value))
            self.logger.debug("")
            
        self.logger.debug("Configuration loaded.")

    # query string 
    # "Q\t%s\t%s\t%s\t%s\t%s\t%s" % (self.qname, self.qclass, self.qtype,
    # self.id, self.remote_ip, self.local_ip or ''
    def query(self, q):

        answer = [] 
        
        if not PARSE_CONFIG_ONCE:
            self.parse_conf()

        for section in sorted(self.cfg.sections(),key=self._keyfunc):
            self.logger.debug ("checking against [%s] section" % section)
            sec_prio, sec_name, sec_type, sec_remote, sec_local = section.split(":")
            name_re = re.compile(sec_name)
            remote_ip = IP(sec_remote)
            local_ip = IP(sec_local)

            # test if query name match against configuration file
            if name_re.search(q.qname):
                self.logger.debug("query name matched: %s => %s" % (q.qname,
                    sec_name))
            else:
                self.logger.debug("query name NOT matched: %s => %s" % (q.qname,
                    sec_name))
                continue
            
            # match if query type match against configuration file
            if q.qtype == sec_type or sec_type == "*":
                self.logger.debug("query type matched: %s => %s" %
                    (q.qtype, sec_type))
            else:
                self.logger.debug("query type NOT matched: %s => %s" %
                    (q.qtype, sec_type))
                continue
            
            # match if remote ip match against configuration type
            if q.remote_ip in remote_ip:
                self.logger.debug("remote ip matched: %s => %s" %
                    (q.remote_ip, remote_ip))
            else:
                self.logger.debug("remote ip NOT matched: %s => %s" %
                    (q.remote_ip, remote_ip))
                continue
            
            # match if local ip match against configuration type
            if q.local_ip:
                if q.local_ip in local_ip:
                    self.logger.debug("local ip matched: %s => %s" %
                        (q.local_ip, local_ip))
                else:
                    self.logger.debug("local ip NOT matched: %s => %s" %
                        (q.local_ip, local_ip))
                    continue
            
            self.logger.debug("Found in section [%s]" % section)
            
            # load specified backend
            if self.cfg.get(section, 'backend') == "ldap":
                self.logger.debug("Found ldap backend")
                ldap_backend = LDAPDNSBackend(self.cfg.items(section),
                        self.logger)
                answer = ldap_backend.query(q)
                break
            if self.cfg.get(section, 'backend') == "static":
                self.logger.debug("Found static backend")
                static_backend = STATICDNSBackend(self.cfg.items(section),
                        self.logger)
                answer = static_backend.query(q)
                break

            else:
                self.logger.warning("backend not found")
                break

        return answer
    
    # TODO: AXFR not implemented !
    def axfr(self, a=None, b=None, c=None):
        self.logger.warning("AXFR query not implemented")
        return []

def main():    

    logger = logging.getLogger('fleppyb')
    if LOGFILE:
        hdlr = logging.FileHandler(LOGFILE)
    else:
        hdlr = logging.StreamHandler()

    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    hdlr.setFormatter(formatter)
    logger.addHandler(hdlr)

    if DEBUG:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    #if not os.path.isfile(CONFIG_FILE):
    #    logger.error("File %s not exist" % CONFIG_FILE)
    #    sys.exit(-1)


    #fleppy_backend = FleppyBackend(logger)
    fleppy_backend = RobotmanBackend(logger)
    backend = PowerDNSBackend(fleppy_backend, logger)
    backend.run()

# Execution
if __name__ == '__main__':
    main()
