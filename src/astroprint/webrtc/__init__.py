# coding=utf-8
__author__ = "Daniel Arroyo <daniel@astroprint.com>"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'

# singleton
_instance = None

def webRtcManager():
	global _instance
	if _instance is None:
		_instance = WebRtc()
	return _instance

import logging
import threading
import subprocess
import json
import os
import signal
import re
import time
import uuid

import astroprint.util as processesUtil

from octoprint.settings import settings
from astroprint.webrtc.janus import Plugin, Session, KeepAlive
from astroprint.boxrouter import boxrouterManager
from astroprint.camera import cameraManager


class WebRtc(object):
	def __init__(self):
		self._connectedPeers = {}
		self._logger = logging.getLogger(__name__)
		self._peerCondition = threading.Condition()
		self._JanusProcess = None
		self.videoId = 1

	def ensureJanusRunning(self):
		if len(self._connectedPeers.keys()) <= 0:
			return self.startJanus()
		else:
			return True #Janus was running before it

	def ensureGstreamerRunning(self):
		cam = cameraManager()
		if cam.open_camera():
			if not cam.start_video_stream():
				self._logger.error('Managing Gstreamer error in WebRTC object')
				#Janus is forced to be closed
				self.stopJanus()
				return False
			return False
		else:
			return True	

	def startLocalSession(self):
		with self._peerCondition:
			sessionId = uuid.uuid4().hex
			
			self._connectedPeers[sessionId] = "local";
			return sessionId

	def closeLocalSession(self, sessionId):
		with self._peerCondition:

			if len(self._connectedPeers.keys()) > 0:
				try:
					peer = self._connectedPeers[sessionId]
				except KeyError:
					self._logger.warning('Session [%s] for peer not found' % sessionId)
					peer = None

				if peer:
					del self._connectedPeers[sessionId]

				self._logger.info("There are %d peers left.", len(self._connectedPeers))

				if len(webRtcManager()._connectedPeers.keys()) == 0:
					#last session
					self.stopGStreamer()
					self.stopJanus()

	def startPeerSession(self, clientId):
		with self._peerCondition:

			if len(self._connectedPeers.keys()) == 0:
				#first session
				self.startJanus()

			peer = ConnectionPeer(clientId, self)
			sessionId = peer.start()
			
			if sessionId:
				self._connectedPeers[sessionId] = peer
				return sessionId

			else:
				#something went wrong, no session started. Do we still need Janus up?
				if len(self._connectedPeers.keys()) == 0:
					self.stopGStreamer()
					self.stopJanus()

				return None

	def closePeerSession(self, sessionId):
		with self._peerCondition:
			if len(self._connectedPeers.keys()) > 0:

				try:
					peer = self._connectedPeers[sessionId]
				except KeyError:
					self._logger.warning('Session [%s] for peer not found' % sessionId)
					peer = None

				if peer:
					peer.streamingPlugin.send_message({'request':'destroy'})
					peer.close()
					self.sendEventToPeer('stopConnection',self._connectedPeers[sessionId])
					del self._connectedPeers[sessionId]

				self._logger.info("There are %d peers left.", len(self._connectedPeers))

				if len(self._connectedPeers.keys()) == 0:
					#last session
					self.stopGStreamer()
					self.stopJanus()
				
	def preparePlugin(self, sessionId):
		
		try:
			peer = self._connectedPeers[sessionId]

		except KeyError:
			self._logger.warning('Peer with session [%s] is not found' % sessionId)
			peer = None
			self.sendEventToPeers('stopConnection',None)

		if peer:
			peer.streamingPlugin.send_message({'request':'list'})
			
			videoEncodingSelected = settings().get(["camera", "encoding"])
						
			if videoEncodingSelected == 'h264':
				
				self.videoId = 1
				
			else:#VP8
				
				self.videoId = 2
			
			self._connectedPeers[sessionId].streamingPlugin.send_message({'request':'watch','id':self.videoId})

	def setSessionDescriptionAndStart(self, sessionId, data):

		try:
			peer = self._connectedPeers[sessionId]

		except KeyError:
			self._logger.warning('Peer with session [%s] is not found' % sessionId)
			peer = None
			self.sendEventToPeers('stopConnection')

		if peer:
			#Janus starting state
			self._connectedPeers[sessionId].streamingPlugin.set_session_description(data['type'],data['sdp'])
			self._connectedPeers[sessionId].streamingPlugin.send_message({'request':'start'})
			#able to serve video
			self.startGStreamer()


	def tickleIceCandidate(self, sessionId, candidate, sdp_mid, sdp_mline_index):
		self._connectedPeers[sessionId].streamingPlugin.add_ice_candidate(candidate, sdp_mid, sdp_mline_index)

	def startGStreamer(self):
		#Start Gstreamer
		if not cameraManager().start_video_stream():
			self._logger.error('Managing Gstreamer error in WebRTC object')
			self.stopJanus()

	def startJanus(self):
		#Start janus command here
		args = ['/opt/janus/bin/./janus']

		try:
			self._JanusProcess = subprocess.Popen(
		    	args,
		    	stdout=subprocess.PIPE
			)

		except Exception, error:
			self._logger.error("Error initiating janus process: %s" % str(error))
			self._JanusProcess = None
			self.sendEventToPeers('stopConnection')
			return False

		if self._JanusProcess:
			while True:
				if 'HTTP/Janus sessions watchdog started' in self._JanusProcess.stdout.readline():
					time.sleep(3)
					break
			return True

	def stopJanus(self):
		
		try:
			if self._JanusProcess is not None:
				self._JanusProcess.kill()
				self.sendEventToPeers('stopConnection')
				return True
		except Exception, error:
			self._logger.error("Error stopping Janus: it is already stopped. Error: %s" % str(error))
			return False
			

	def stopGStreamer(self):
		cameraManager().stop_video_stream()
	
	def sendEventToPeers(self, type, data=None):
		
		for key in self._connectedPeers.keys():
			boxrouterManager().send({
				'type': 'send_event_to_client',
				'data': {
					'clientId': self._connectedPeers[key].clientId,
					'eventType': type,
					'eventData': data
				}
			})
			
	def sendEventToPeer(self, type, data=None):
		boxrouterManager().send({
			'type': 'send_event_to_client',
			'data': {
				'clientId': data.clientId,
				'eventType': type,
				'eventData': None
			}
		})

class StreamingPlugin(Plugin):
	name = 'janus.plugin.streaming'

class ConnectionPeer(object):

	def __init__(self, clientId, parent):
		self.session = None
		self.clientId = clientId
		self.sessionKa = None
		self.id = None
		self.streamingPlugin = None
		self._parent = parent

	#CONNECTION
	#def connection_on_opened(self,connection):
	#	logging.info('CONNECTION ON OPENED')

	#def connection_on_closed(self,connection,**kw):
	#	logging.info('CONNECTION ON CLOSED')

	def connection_on_message(self,connection,message):

		messageToReturn = json.loads(str(message))

		if self.session is not None and 'session_id' in messageToReturn and messageToReturn['session_id'] != self.session.id:
			return

		if 'janus' in messageToReturn and messageToReturn['janus'] == 'hangup':
			#Finish camera session caused by user or exception
			self._parent.closePeerSession(messageToReturn['session_id'])
		elif 'jsep' in messageToReturn:
			self.sendEventToPeer('getSdp',messageToReturn)

	#SESSION
	#def session_on_connected(self,session,**kw):
	#	logging.info('SESSION ON OPENED')

	#def session_on_disconnected(self,session,**kw):
	#	logging.info('SESSION ON CLOSED')

	#def session_on_message(self,session,**kw):
	#	logging.info('SESSION ON MESSAGE')

	#def session_on_plugin_attached(self,session,**kw):
	#	logging.info('SESSION ON PLUGIN ATTACHED')

	#def session_on_plugin_detached(self,session,**kw):
	#	logging.info('SESSION ON PLUGIN DETACHED')


	#PLUGIN
	#def streamingPlugin_on_message(self,plugin,**kw):
	#	logging.info('STREAMINGPLUGIN ON MESSAGE')

	#def streamingPlugin_on_attached(self,plugin,**kw):
	#	logging.info('STREAMINGPLUGIN ON ATTACHED')

	#def streamingPlugin_on_detached(self,plugin,**kw):
	#	logging.info('STREAMINGPLUGIN ON DETACHED')

	#def streamingPlugin_on_webrtcup(self,plugin,**kw):
	#	logging.info('STREAMINGPLUGIN ON WEBRTCUP')

	#def streamingPlugin_on_hangup(self,plugin,**kw):
	#	logging.info('STREAMINGPLUGIN ON HANGUP')

	def start(self):

		sem = threading.Semaphore(0)

		self.streamingPlugin = StreamingPlugin()
		self.session = Session('ws://127.0.0.1:8188', secret='d5faa25fe8e3438d826efb1cd3369a50')

		@self.session.on_plugin_attached.connect
		def receive_data(sender, **kw):
			#wait until Janus plugin is not attached
			sem.release()


		#CONNECTION
		#self.session.cxn_cls
		#
		#SIGNALS
		#
		# Signal fired when `Connection` has been established.
	    #	on_opened = blinker.Signal()
		#self.session.cxn_cls.on_opened.connect(self.connection_on_opened)

		#
	    # Signal fired when `Connection` has been closed.
	    #	on_closed = blinker.Signal()
		#self.session.cxn_cls.on_closed.connect(self.connection_on_closed)

		#
	    # Signal fired when `Connection` receives a message
	    #	on_message = blinker.Signal()
		self.session.cxn_cls.on_message.connect(self.connection_on_message)

	    ##


	    #SESSION
	    #self.session
	    #
	    #SIGNALS
	    #
	    # Signal fired when `Session` has been connected.
	    #	on_connected = blinker.Signal()
		#self.session.on_connected.connect(self.session_on_connected)

		#
		# Signal fired when `Session` has been disconnected.
		#	on_disconnected = blinker.Signal()
		#self.session.on_disconnected.connect(self.session_on_disconnected)

		#
		# Signal fired when a `Session` level message is received.
		#	on_message = blinker.Signal()
		#self.session.on_message.connect(self.session_on_message)

		#
		# Signal fired when a `Session` `Plugin` been attached.
		#	on_plugin_attached = blinker.Signal()
		#self.session.on_plugin_attached.connect(self.session_on_plugin_attached)

		#
		# Signal fired when a `Session` `Plugin` been detached.
		#	on_plugin_detached = blinker.Signal()
		#self.session.on_plugin_detached.connect(self.session_on_plugin_detached)

		##


		#PLUGIN
		#self.streamingPlugin
		#
		#SIGNALS
		#
		# Signal fired when a `Plugin` is attached to a `Session`.
		#	on_attached = blinker.Signal()
		#self.streamingPlugin.on_attached.connect(self.streamingPlugin_on_attached)

		#
		# Signal fired when a `Plugin` is attached to a `Session`.
		#	on_detached = blinker.Signal()
		#self.streamingPlugin.on_detached.connect(self.streamingPlugin_on_detached)

		#
		# Signal fired when a `Plugin` receives a message.
		#	on_message = blinker.Signal()
		#self.streamingPlugin.on_message.connect(self.streamingPlugin_on_message)
		#

		# Signal fired when webrtc for a `Plugin` has been setup.
		#	on_webrtcup = blinker.Signal()
		#self.streamingPlugin.on_webrtcup.connect(self.streamingPlugin_on_webrtcup)


		#
		# Signal fired when webrtc session for a `Plugin` has been torn down.
		#	on_hangup = blinker.Signal()
		#self.streamingPlugin.on_hangup.connect(self.streamingPlugin_on_hangup)

		##

		self.session.register_plugin(self.streamingPlugin);
		self.session.connect();


		self.sessionKa = KeepAlive(self.session)
		self.sessionKa.daemon = True
		self.sessionKa.start()

		sem.acquire()
		
		return self.session.id

	def close(self):
		#stop the keepalive worker
		self.sessionKa.stop()
		self.sessionKa.join()
		self.sessionKa = None

		#kill the current session
		self.session.unregister_plugin(self.streamingPlugin)
		self.session.disconnect()
		self.session = None
		self.streamingPlugin = None

	def sendEventToPeer(self, type, data):
		boxrouterManager().send({
			'type': 'send_event_to_client',
			'data': {
				'clientId': self.clientId,
				'eventType': type,
				'eventData': data
			}
		})
	
	"""def __init__(self):
		self._logger = logging.getLogger(__name__)
		
	def startJanusSec(self):
		
		if len(webRtcManager()._connectedPeers.keys()) <= 0:
			if webRtcManager().startJanus():
				return 1#Janus starts
			else:
				return 0#Janus can not start
		else:
			return 2#Janus was running before it

	def startGstreamer(self):
		#Start Gstreamer
		cam = cameraManager()
		if cam.open_camera():
			if not cam.start_video_stream():
				self._logger.error('Managing Gstreamer error in WebRTC object')
				#Janus is forced to be closed
				webRtcManager().stopJanus()
				return False
			return False
		else:
			return True

	def startPeerSession(self, clientId):
		with webRtcManager()._peerCondition:

			peer = ConnectionPeer(clientId, self)
			sessionId = peer.start()
			
			if sessionId:
				webRtcManager()._connectedPeers[sessionId] = peer
				return sessionId

			else:
				#something went wrong, no session started. Do we still need Janus up?
				if len(webRtcManager()._connectedPeers.keys()) == 0:
					self.stopGStreamer()
					self.stopJanus()

				return None

	def closePeerSession(self, sessionId):
		with webRtcManager()._peerCondition:

			if len(webRtcManager()._connectedPeers.keys()) > 0:

				try:
					logging.info(webRtcManager()._connectedPeers)
					peer = webRtcManager()._connectedPeers[sessionId]
				except KeyError:
					webRtcManager()._logger.warning('Session [%s] for peer not found' % sessionId)
					peer = None

				if peer:
					peer.streamingPlugin.send_message({'request':'destroy'})
					peer.close()
					del webRtcManager()._connectedPeers[sessionId]

				if len(webRtcManager()._connectedPeers.keys()) == 0:
					#last session
					webRtcManager().stopGStreamer()
					webRtcManager().stopJanus()
	"""
