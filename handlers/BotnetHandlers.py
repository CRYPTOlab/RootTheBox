# -*- coding: utf-8 -*-
'''
Created on Mar 15, 2012

@author: moloch

    Copyright 2012 Root the Box

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.
'''

import os
import json
import logging
import tornado.websocket


from uuid import uuid4
from hashlib import sha1
from libs.BotManager import BotManager
from libs.EventManager import EventManager
from models import Box, Team, User
from models.User import ADMIN_PERMISSION
from BaseHandlers import BaseHandler, BaseWebSocketHandler
from libs.SecurityDecorators import *


class BotSocketHandler(tornado.websocket.WebSocketHandler):
    ''' 

    *** Rough bot protocol layout ***
    =================================
    1) Bot connects to server
        a) If IP config.whitelist_box_ips is enabled, check
           the datbase for boxes with matching IPs

    2) Servers responds with "Interrogation" message
        a) This message includes a random string 'xid'

    3) Bot responds with a "InterrogationResponse", includes
        a) The value of SHA1(xid + box garbage)
        b) Asserted user handle (reward goes to user.team)
        c) Asserted box name

    4) Server looks up asserted box and user in database, ensures
        they do exist, and the user is not an admin.  
    
    5) Server then computes it's own SHA1(xid + box garbage)
        a) Check if the server's value matches the bot's

    6) Check for duplicate bots (one bot per box per team)

    7) Add new bot to botnet

    '''

    def initialize(self):
        self.bot_manager = BotManager.Instance()
        self.event_manager = EventManager.Instance()
        self.team_name = None
        self.team_uuid = None
        self.box_uuid = None
        self.remote_ip = None
        self.xid = os.urandom(16).encode('hex')
        self.uuid = unicode(uuid4())
        self.opcodes = {
            'interrogation_response': self.interrogation_response,
        }
    
    def open(self, *args):
        ''' Steps 1 and 2; called when a new bot connects '''
        box = Box.by_ip_address(self.request.remote_ip)
        self.remote_ip = self.request.remote_ip
        if box is None and self.config.whitelist_box_ips:
            logging.debug("Rejected bot from '%s' (not a box)" % self.request.remote_ip)
            self.write_message({
                'opcode': 'error',
                'message': 'Invalid IP address.'
            })
            self.close()
        else:
            self.write_message({'opcode': 'interrogate', 'xid': self.xid})

    def interrogation_response(self, msg):
        ''' Steps 3 and 4; validate repsonses '''
        response_xid = msg['rxid']
        user = User.by_handle(msg['handle'])
        box = Box.by_name(msg['box_name'])
        if self.config.whitelist_box_ips and self.remote_ip not in box.ips:
            self.send_error("Invalid remote IP for this box")
        elif user is None or user.has_permission(ADMIN_PERMISSION):
            self.send_error("User does not exist")
        elif box is None:
            self.send_error("Box does not exist")
        elif not self.is_valid_xid(box, response_xid):
            self.send_error("Invalid xid response")
        else:
            self.team_name = user.team.name
            self.team_uuid = user.team.uuid
            self.box_uuid = box.uuid
            self.add_to_botnet()

    def add_to_botnet(self):
        ''' Step 6 and 7; Add current web socket to botnet '''
        if self.bot_manager.add_bot(self):
            logging.debug("Auth okay, adding '%s' to botnet" % self.uuid)
            count = self.bot_manager.count_by_team(self.team_name)
            self.write_message({
                'opcode': 'status',
                'message': 'Added new bot; total number of bots is now %d' % count
            })
        else:
            logging.debug("Duplicate bot on %s" % self.remote_ip)
            self.send_error("Duplicate bot")

    def is_valid_xid(box, response_xid):
        sha = sha1()
        sha.update(self.xid + box.garbage)
        return response_xid == sha.hexdigest()

    def ping(self):
        ''' Just make sure we can write data to the socket '''
        try:
            self.write_message({'opcode': 'ping'})
        except:
            logging.exception("Error: while sending ping to bot.")
            self.close()

    def send_error(self, msg):
        ''' Send the errors, and close socket '''
        self.write_message({
            'opcode': 'error',
            'message': msg,
        })
        self.close()

    def on_message(self, message):
        ''' Routes the request to the correct function based on opcode '''
        try:
            req = json.loads(message)
            if 'opcode' not in req:
                raise ValueError('Missing opcode')
            elif req['opcode'] not in self.opcodes:
                raise ValueError('Invalid opcode in request: %s' % req['opcode'])
            else:
                self.opcodes[req['opcode']](req)
        except ValueError as error:
            logging.warn("Invalid json request from bot: %s" % str(error))
            self.close()

    def on_close(self):
        ''' Close connection to remote host '''
        if self.uuid in self.bot_manager.botnet:
            self.bot_manager.remove_bot(self)
        logging.debug("Closing connection to bot at %s" % self.request.remote_ip)



class BotCliMonitorSocketHandler(tornado.websocket.WebSocketHandler):
    ''' 
    Handles the CLI BotMonitor websocket connections, has custom auth.
    TODO: Trash this and use the web api handler, w/ normal session cookie
    '''

    def initialize(self):
        self.bot_manager = BotManager.Instance()
        self.team_name = None
        self.uuid = unicode(uuid4())
        self.opcodes = {
            'auth': self.auth,
        }

    def open(self):
        logging.debug("Opened new monitor socket to %s" % self.request.remote_ip)

    def on_message(self, message):
        ''' Parse request '''
        try:
            req = json.loads(message)
            if 'opcode' not in req:
                raise ValueError('Missing opcode')
            elif req['opcode'] not in self.opcodes:
                raise ValueError('Invalid opcode in request: %s' % req['opcode'])
            else:
                self.opcodes[req['opcode']](req)
        except ValueError as error:
            logging.warn("Invalid json request from bot: %s" % str(error))

    def on_close(self):
        ''' Close connection to remote host '''
        self.bot_manager.remove_monitor(self)
        logging.debug("Closing connection to bot monitor at %s" % self.request.remote_ip)

    
    def auth(self, req):
        ''' Authenticate user '''
        try:
            user = User.by_handle(req['handle'])
        except:
            user = None
        if user is None or user.has_permission(ADMIN_PERMISSION):
            logging.debug("Monitor socket user does not exist.")
            self.write_message({
                'opcode': 'auth_failure',
                'message': 'Authentication failure',
            })
            self.close()
        elif user.validate_password(req.get('password', '')):
            logging.debug("Monitor socket successfully authenticated as %s" % user.handle)
            self.team_name = ''.join(user.team.name)
            self.bot_manager.add_monitor(self)
            self.write_message({'opcode': 'auth_success'})
            bots = self.bot_manager.get_bots(self.team_name)
            self.update(bots)
        else:
            logging.debug("Monitor socket provided invalid password for user")
            self.write_message({
                'opcode': 'auth_failure',
                'message': 'Authentication failure',
            })
            self.close()

    def update(self, bots):
        ''' Called by the observable class '''
        self.write_message({'opcode': 'update', 'bots': bots})

    def ping(self):
        self.write_message({'opcode': 'ping'})


class BotWebMonitorHandler(BaseHandler):
    ''' Just renders the html page for the web monitor '''

    @authenticated
    def get(self, *args, **kwargs):
        self.render('bots/monitor.html')
    

class BotWebMonitorSocketHandler(BaseWebSocketHandler):
    ''' 
    Bot monitor API, requires user to be authenticated with the web app 
    TODO: Move the cli api to use this one.

    This class implements an observer pattern, and registers itself with 
    the BotManager class.

    It must have:
        cls.uuid
        cls.team_name
        cls.ping

    '''

    @restrict_origin
    def open(self):
        ''' Only open sockets from authenticated clients '''
        if self.session is not None and 'team_id' in self.session:
            user = self.get_current_user()
            logging.debug("[Web Socket] Opened web monitor socket with %s" % user.handle)
            self.uuid = unicode(uuid4())
            self.bot_manager = BotManager.Instance()
            self.team_name = ''.join(user.team.name)
            self.bot_manager.add_monitor(self)
            bots = self.bot_manager.get_bots(self.team_name)
            self.update(bots)
        else:
            logging.debug("[Web Socket] Denied web monitor socket to %s" % self.request.remote_ip)
            self.bot_manager = None
            self.close()

    def on_message(self, message):
        user = self.get_current_user()
        logging.debug("%s is send us websocket messages." % user.handle)

    def update(self, boxes):
        ''' Called by observable class '''
        self.write_message({'opcode': 'update', 'bots': boxes})

    def ping(self):
        ''' Send an update as a ping '''
        bots = self.bot_manager.get_bots(self.team_name)
        self.update(bots)

    def on_close(self):
        ''' Close connection to remote host '''
        if self.bot_manager is not None:
            self.bot_manager.remove_monitor(self)


class BotDownloadHandler(BaseHandler):
    ''' Distributes bot binaries / scripts '''

    @authenticated
    def get(self, *args, **kwargs):
        download_options = {
            'windows': self.windows,
            'linux': self.generic,
            'monitor': self.monitor,
        }
        if len(args) == 1 and args[0] in download_options:
            download_options[args[0]]()
        self.finish()

    def windows(self):
        ''' Download Windows PE file '''
        self.set_header("Content-Type", "application/exe")
        self.set_header("Content-disposition", "attachment; filename=rtb_bot.exe")
        if os.path.exists('bot/dist/bot.exe'):
            f = open('bot/dist/bot.exe')
            data = f.read()
            self.set_header('Content-Length', len(data))
            self.write(data)
            f.close()
        else:
            logging.error("Missing Windows bot file, please run build script: bot/build_bot.py")
            self.generic()

    def generic(self):
        ''' Send them the generic python script '''
        self.set_header("Content-Type", "text/x-python"); 
        self.set_header("Content-disposition", "attachment; filename=rtb_bot.py")
        if os.path.exists('bot/bot.py'):
            f = open('bot/bot.py')
            data = f.read()
            self.set_header('Content-Length', len(data))
            self.write(data)
            f.close()

    def monitor(self):
        ''' Send curses ui bot monitor '''
        self.set_header("Content-Type", "text/x-python"); 
        self.set_header("Content-disposition", "attachment; filename=botnet_monitor.py")
        if os.path.exists('bot/BotMonitor.py'):
            f = open('bot/BotMonitor.py')
            data = f.read()
            self.set_header('Content-Length', len(data))
            self.write(data)
            f.close()



