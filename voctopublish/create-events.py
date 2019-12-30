#!/usr/bin/env python3
#    Copyright (C) 2017  derpeter <derpeter@berlin.ccc.de>
#    Copyright (C) 2019  andi <andi@muc.ccc.de>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

import configparser
import socket
import sys
import logging
import os
import subprocess
import argparse 
import time

from api_client.c3tt_rpc_client import C3TTClient
from api_client.voctoweb_client import VoctowebClient
from model.ticket_module import Ticket

 

class RelivePublisher:
    """
    This is the main class for the Voctopublish application
    It is meant to be used with the c3tt ticket tracker
    """
    def __init__(self, args = {}):
        # load config
        if not os.path.exists('client.conf'):
            raise IOError("Error: config file not found")

        self.config = configparser.ConfigParser()
        self.config.read('client.conf')

        self.notfail = args.notfail

        # set up logging
        logging.addLevelName(logging.WARNING, "\033[1;33m%s\033[1;0m" % logging.getLevelName(logging.WARNING))
        logging.addLevelName(logging.ERROR, "\033[1;41m%s\033[1;0m" % logging.getLevelName(logging.ERROR))
        logging.addLevelName(logging.INFO, "\033[1;32m%s\033[1;0m" % logging.getLevelName(logging.INFO))
        logging.addLevelName(logging.DEBUG, "\033[1;85m%s\033[1;0m" % logging.getLevelName(logging.DEBUG))

        self.logger = logging.getLogger()

        sh = logging.StreamHandler(sys.stdout)
        if self.config['general']['debug']:
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s {%(filename)s:%(lineno)d} %(message)s')
        else:
            formatter = logging.Formatter('%(asctime)s - %(message)s')

        sh.setFormatter(formatter)
        self.logger.addHandler(sh)
        self.logger.setLevel(logging.DEBUG)

        level = self.config['general']['debug']
        if level == 'info':
            self.logger.setLevel(logging.INFO if not args.verbose else logging.DEBUG)
        elif level == 'warning':
            self.logger.setLevel(logging.WARNING)
        elif level == 'error':
            self.logger.setLevel(logging.ERROR)
        elif level == 'debug':
            self.logger.setLevel(logging.DEBUG)

        if self.config['C3Tracker']['host'] == "None":
            self.host = socket.getfqdn()
        else:
            self.host = self.config['C3Tracker']['host']

        self.ticket_type = 'encoding'
        self.to_state = 'releasing'

        logging.debug('creating C3TTClient')
        try:
            self.c3tt = C3TTClient(self.config['C3Tracker']['url'],
                                   self.config['C3Tracker']['group'],
                                   self.host,
                                   self.config['C3Tracker']['secret'])
        except Exception as e_:
            raise PublisherException('Config parameter missing or empty, please check config') from e_

    def create_event(self):
        """
        Decide based on the information provided by the tracker where to publish.
        """
        ticket = self._get_ticket_from_tracker()

        if not ticket:
            return

        # voctoweb
        if ticket.profile_voctoweb_enable and ticket.voctoweb_enable:
            logging.debug(
                'encoding profile media flag: ' + str(ticket.profile_voctoweb_enable) + " project media flag: " + str(ticket.voctoweb_enable))
            self._publish_event_to_voctoweb(ticket)


    def _get_ticket_from_tracker(self):
        """
        Request the next unassigned ticket for the configured states
        :return: a ticket object or None in case no ticket is available
        """
        logging.info('requesting ticket from tracker')
        t = None

        ticket_meta = None
        # when we are in debug mode, we first check if we are already assigned to a ticket from previous run
        if self.notfail:
            ticket_meta = self.c3tt.get_assigned_for_state(self.ticket_type, self.to_state, {'EncodingProfile.Slug': 'relive'})
        # otherwhise, or if that was not successful get the next unassigned one
        if not ticket_meta:
            ticket_meta = self.c3tt.assign_next_unassigned_for_state(self.ticket_type, self.to_state, {'EncodingProfile.Slug': 'relive'})
        
        if ticket_meta:
            ticket_id = ticket_meta['id']
            logging.info("Ticket ID:" + str(ticket_id))
            try:
                ticket_properties = self.c3tt.get_ticket_properties(ticket_id)
                logging.debug("Ticket Properties: " + str(ticket_properties))
            except Exception as e_:
                if not args.notfail:
                    self.c3tt.set_ticket_failed(ticket_id, e_)
                raise e_
            t = Ticket(ticket_meta, ticket_properties)
        else:
            logging.info('No ticket of type ' + self.ticket_type + ' for state ' + self.to_state)

        return t

    def _publish_event_to_voctoweb(self, ticket):
        """
        Create a event on an voctomix instance. This includes creating a recording for each media file.
        """
        try:
            vw = VoctowebClient(ticket,
                                self.config['voctoweb']['api_key'],
                                self.config['voctoweb']['api_url'],
                                self.config['voctoweb']['ssh_host'],
                                self.config['voctoweb']['ssh_port'],
                                self.config['voctoweb']['ssh_user'])
        except Exception as e_:
            raise PublisherException('Error initializing voctoweb client. Config parameter missing') from e_

        if not(ticket.voctoweb_event_id):
            # if this is master ticket we need to check if we need to create an event on voctoweb

            # check if event exists on voctoweb instance, and abort if this is already the case
            r = vw.get_event()
            if r.status_code == 204:
                print(r.content)
                print(r.status_code)
                #self.c3tt.set_ticket_done(ticket)
                print('event already exists, abort!')
                print(os.path.join(ticket.voctoweb_url, ticket.slug))
                return


            logging.debug('this is a master ticket')
            r = vw.create_or_update_event()
            if r.status_code in [200, 201]:
                logging.info("new event created or existing updated")
                
                '''
                try:
                    # we need to write the Event ID onto the parent ticket, so the other (master) encoding tickets 
                    # also have acccess to the Voctoweb Event ID
                    self.c3tt.set_ticket_properties(ticket.parent_id, {'Voctoweb.EventId': r.json()['id']})
                except Exception as e_:
                    raise PublisherException('failed to Voctoweb EventID to parent ticket') from e_
                '''

            elif r.status_code == 422:
                # If this happens tracker and voctoweb are out of sync regarding the event id
                # or we have some input error...
                # todo: write voctoweb event_id to ticket properties --Andi
                logging.warning("event already exists => please sync event manually")
            else:
                raise PublisherException('Voctoweb returned an error while creating an event: ' + str(r.status_code) + ' - ' + str(r.content))
            
            print(os.path.join(ticket.voctoweb_url, ticket.slug))
        else:
            logging.info("nothing to to, is already pubilshed")
            print(os.path.join(ticket.voctoweb_url, ticket.slug))
            print("master? ", ticket.master )
            print("event id:", ticket.voctoweb_event_id)



        self.c3tt.set_ticket_done(ticket)


class PublisherException(Exception):
    pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='generate events on voctoweb for relive ') 
    parser.add_argument('--verbose',  '-v', action='store_true', default=False)
    parser.add_argument('--notfail', action='store_true', default=False, help='do not mark ticket as failed in tracker when something goes wrong')

    args = parser.parse_args() 

    try:
        publisher = RelivePublisher(args)
    except Exception as e:
        logging.error(e)
        logging.exception(e)
        sys.exit(-1)

    try:
        publisher.create_event()
    except Exception as e:
        exc_type, exc_obj, exc_tb = sys.exc_info()
        logging.exception(e)
        sys.exit(-1)
