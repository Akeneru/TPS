import socket
import thread
import logging
import select
import struct
import time, random


from connectioninfo import ConnectionInfo
from connection import ConnectionManager
from messagehandlerservice import MessageHandlerService
from message import Message
from messagesender import *
from util.tasks import PeriodicExecutor
from util.timer import Timer

HEADER_FORMAT = '<i' # little endian integer
PROTOCOL_VERSION = 12
SERVER_VERSION = "Terraria" + str(PROTOCOL_VERSION)
MESSAGE_TYPE_FORMAT = '<B' # little endian byte (char)
FPS = 60

log = logging.getLogger()

class NetworkState:
  Starting = 0
  Running = 1
  Closing = 2
  Closed = 3
  Error = 4

class TerrariaServer:

  def __init__(self, listenAddr, listenPort, world, password=None):
    self.listenAddress = listenAddr
    self.listenPort = listenPort
    self.password = password
    self.motd = "Welcome to the jungle! We got fun and games!"
    self.world = world
    self.networkState = NetworkState.Closed
    self.connectionManager = ConnectionManager()
    self.messageSender = MessageSender(self.connectionManager)
    self.messageHandlerService = MessageHandlerService(self, self.messageSender)
#    self.updateServerTask = PeriodicExecutor(60, self.__updateServer, ())
    self.world.onItemCreated.addHandler(self.__itemCreatedEventHandler)
    self.world.onProjectileCreated.addHandler(self.__projectileCreatedEventHandler)
    self.world.onNewTileSquare.addHandler(self.__newTileSquareEventHandler)
    self.isRunning = False
    self.updateTimer = Timer()
    self.fpsTimer = Timer()
    
  def __itemCreatedEventHandler(self, eventArgs):
    """
    Occurs when the world creates a new item (e.g. by destroying a tile)
    """
    item = eventArgs.item
    itemNum = eventArgs.itemNumber
    if item:
      itemInfoMessage = self.messageSender.messageBuilder.buildItemInfoMessage(itemNum, item.position[0], item.position[1], item.velocity[0], item.velocity[1], item.stackSize, item.itemName)
      self.messageSender.sendMessageToAllClients(itemInfoMessage)
      
  def __newTileSquareEventHandler(self, eventArgs):
    """
    Occurs when the world needs to send a new tile square event
    """
    self.messageSender.sendTileSquareMessageToAllClients(eventArgs.tileX, eventArgs.tileY, eventArgs.size, self.world)
      
  def __projectileCreatedEventHandler(self, projectile):
    """
    Occurs when the world creates a new projectile (e.g. when sand is destroyed
    and sand is above the destroyed sand tile, the sand needs to fall down, so
    a projectile is created).
    """
    if projectile:
      self.messageSender.sendProjectileMessageToAllClients(projectile)
    else:
      log.debug("projectile=None")

  def __setupSocket(self):
    try:
      self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
      self.socket.bind((self.listenAddress, self.listenPort))
      self.socket.listen(5)
      log.debug("Server listening on " + str(self.listenAddress))
      self.networkState = NetworkState.Running
    except Exception as ex:
      log.error(ex)
      self.networkState = NetworkState.Error

  def __doProtocol(self, connection):
    try:
      header = connection.socket.recv(4) # first 4 bytes tell us the message length
      if len(header) < 4:
        log.debug("Got invalid header...disconnecting client ")
        self.connectionManager.removeConnection(connection)
        self.messageSender.sendPlayerDisconnectedToOtherClients(connection)
        return
      msgLen = struct.unpack(HEADER_FORMAT, header)[0] # unpack returns a tuple 
      connection.data = connection.socket.recv(msgLen)
      # Now get the rest of the message from the client....
      # first byte of the data is the message Type
      messageType = struct.unpack(MESSAGE_TYPE_FORMAT, connection.data[0])[0]
      message = Message(messageType)
      message.appendRaw(connection.data[1:])
      self.messageHandlerService.processMessage(message, connection)
    except Exception as ex:
      log.error(ex)
      self.connectionManager.removeConnection(connection)

  def __readThread(self):
    while self.networkState == NetworkState.Running:
      socketList = self.connectionManager.getListOfSocketsForSelect()
      socketsToRead, socketsToWrite, socketsWithError = select.select(socketList, [], socketList, 0.1)
      for serr in socketsWithError:
        self.connectionManager.removeConnection(self.connectionManager.findConnection(serr))
      for s in socketsToRead:
        self.__doProtocol(self.connectionManager.findConnection(s))

  def __acceptLoop(self):
    while self.networkState == NetworkState.Running:
      (clientsock, clientaddr) = self.socket.accept()
      # New connection here
      log.debug("New connection from " + str(clientaddr))
      connection = ConnectionInfo(clientsock, clientaddr, self.connectionManager.getNewClientId())
      log.debug("Client id: " + str(connection.clientNumber))
      self.connectionManager.addConnection(connection)

  def __updateServer(self):
    self.world.update(3601)
    self.messageSender.sendWorldUpdateToAllClients(self.world)
    self.messageSender.syncPlayers()

  def __mainLoop(self):
    """
    Main game loop to process game entities
    """
    self.updateTimer.start()
    while self.isRunning:
      self.fpsTimer.start()
      self.world.update(1.0)
      if self.updateTimer.getTicks() > 3600.0:
        self.messageSender.sendWorldUpdateToAllClients(self.world)
        self.messageSender.syncPlayers()
        # Restart update timer
        self.updateTimer.start()
      if self.fpsTimer.getTicks() < (1000.0 / FPS):
        sleepTime = (( 1000.0 / FPS ) - self.fpsTimer.getTicks() ) / 1000.0
        time.sleep(sleepTime)
        
  def start(self):
    log.debug("Server starting up...")
    self.__setupSocket()
    # set up a thread to read from the clients sockets
    thread.start_new_thread(self.__readThread, ())
#    thread.start_new_thread(self.updateServerTask.run, ())
    thread.start_new_thread(self.__acceptLoop, ())
    self.isRunning = True
    self.__mainLoop()
