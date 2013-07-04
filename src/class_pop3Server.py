from collections import deque
import asyncore
import shared
import socket
import ssl
import sys

from addresses import *
import helper_inbox

class bitmessagePOP3Connection(asyncore.dispatcher):
    END = b"\r\n"

    def __init__(self, sock, peer_address, debug=False):
        asyncore.dispatcher.__init__(self, sock)
        self.peer_address = peer_address
        self.data_buffer = []
        self.commands    = deque()
        self.debug       = debug

        self.dispatch = dict(
            USER=self.handleUser,
            PASS=self.handlePass,
            STAT=self.handleStat,
            LIST=self.handleList,
            #TOP=self.handleTop,
            RETR=self.handleRetr,
            DELE=self.handleDele,
            NOOP=self.handleNoop,
            QUIT=self.handleQuit,
        )

        self.messages = None
        self.storage_size = 0
        self.address = None
        self.pw = None
        self.loggedin = False
        
        self.sendline("+OK Bitmessage POP3 server ready")

    def populateMessageIndex(self):
        if not self.loggedin:
            raise Exception("Cannot be called when not logged in.")

        if self.address is None:
            raise Exception("Invalid address: {}".format(self.address))

        if self.messages is not None:
            return

        v = (self.address,)
        shared.sqlLock.acquire()
        # TODO LENGTH(message) needs to be the byte-length, not the character-length.
        shared.sqlSubmitQueue.put('''SELECT msgid, fromaddress, subject, LENGTH(message) FROM inbox WHERE folder='inbox' AND toAddress=?''')
        shared.sqlSubmitQueue.put(v)
        queryreturn = shared.sqlReturnQueue.get()
        shared.sqlLock.release()

        self.storage_size = 0
        self.messages = []
        for row in queryreturn:
            msgid, fromAddress, subject, size = row
            subject = shared.fixPotentiallyInvalidUTF8Data(subject)
            if subject.startswith("<Bitmessage Mail: ") and subject[-1] == '>':
                subject = "<Bitmessage Mail: 00000000000000000000>" # Reserved, flags.
                flags = subject[-21:-1]
                # TODO - checksum?

                self.messages.append({
                    'msgid': msgid,
                    'fromAddress': fromAddress,
                    'subject': subject,
                    'size': size,
                })

                self.storage_size += size


    def getMessageContent(self, msgid):
        if self.address is None:
            raise Exception("Invalid address: {}".format(self.address))

        v = (msgid,)
        shared.sqlLock.acquire()
        shared.sqlSubmitQueue.put('''SELECT fromaddress, received, message, encodingtype FROM inbox WHERE msgid=?''')
        shared.sqlSubmitQueue.put(v)
        queryreturn = shared.sqlReturnQueue.get()
        shared.sqlLock.release()

        for row in queryreturn:
            fromAddress, received, message, encodingtype = row
            message = shared.fixPotentiallyInvalidUTF8Data(message)
            return {
                'fromAddress': fromAddress,
                'received': received,
                'message': message,
                'encodingtype': encodingtype
            }

    def trashMessage(self, msgid):
        # TODO - how to determine if the update succeeded?
        helper_inbox.trash(msgid)
        return True

    def sendline(self, data, END=END):
        if self.debug:
            shared.printLock.acquire()
            sys.stdout.write("sending ")
            sys.stdout.write(data)
            sys.stdout.write("\n")
            shared.printLock.release()
        data = data + END
        while len(data) > 4096:
            self.send(data[:4096])
            data = data[4096:]
        if len(data):
            self.send(data)

    def handle_read(self):
        chunk = self.recv(4096)

        while bitmessagePOP3Connection.END in chunk:
            # Join all the data up to the END and throw it in commands
            command = b''.join(self.data_buffer) + chunk[:chunk.index(bitmessagePOP3Connection.END)]
            chunk = chunk[chunk.index(bitmessagePOP3Connection.END)+2:]
            self.data_buffer = []
            self.commands.append(command)

        if len(chunk):
            self.data_buffer.append(chunk)

        if self.debug:
            shared.printLock.acquire()
            print('data_buffer', self.data_buffer)
            print('commands', self.commands)
            print('-')
            shared.printLock.release()

        while len(self.commands):
            line = self.commands.popleft()

            if b' ' in line:
                cmd, data = line.split(b' ', 1)
            else:
                cmd, data = line, b''

            try:
                cmd = self.dispatch[cmd.decode('ascii').upper()]
            except KeyError:
                self.sendline('-ERR unknown command')
                continue

            for response in cmd(data):
                self.sendline(response)

            if cmd is self.handleQuit:
                self.close()
                break

    def handleUser(self, data):
        if self.loggedin:
            raise Exception("Cannot login twice")

        self.address = data

        status, addressVersionNumber, streamNumber, ripe = decodeAddress(self.address)
        if status != 'success':
            shared.printLock.acquire()
            print 'Error: Could not decode address: ' + self.address + ' : ' + status
            if status == 'checksumfailed':
                print 'Error: Checksum failed for address: ' + self.address
            if status == 'invalidcharacters':
                print 'Error: Invalid characters in address: ' + self.address
            if status == 'versiontoohigh':
                print 'Error: Address version number too high (or zero) in address: ' + self.address
            shared.printLock.release()
            raise Exception("Invalid Bitmessage address: {}".format(self.address))

        self.address = addBMIfNotPresent(self.address)

        # Each identity must be enabled independly by setting the smtppop3password for the identity
        # If no password is set, then the identity is not available for SMTP/POP3 access.
        try:
            if shared.config.getboolean(self.address, "enabled"):
                self.pw = shared.config.get(self.address, "smtppop3password")
                yield "+OK user accepted"
                return
        except:
            pass

        yield "-ERR access denied"
        self.close()
    
    def handlePass(self, data):
        if self.pw is None:
            yield "-ERR must specify USER"
        else:
            pw = data.decode('ascii')
            if pw == self.pw: # TODO - hashed passwords?
                yield "+OK pass accepted"
                self.loggedin = True
            else:
                yield "-ERR invalid password"

    def handleStat(self, data):
        self.populateMessageIndex()
        return ["+OK {} {}".format(len(self.messages), self.storage_size)]
    
    def handleList(self, data):
        self.populateMessageIndex()
        yield "+OK {} messages ({} octets)".format(len(self.messages), self.storage_size)
        for i, msg in enumerate(self.messages):
            yield "{} {}".format(i + 1, msg['size'])
        yield "."

    #def handleTop(self, data):
    #    cmd, num, lines = data.split()
    #    assert num == "1", "unknown message number: {}".format(num)
    #    lines = int(lines)
    #    text = msg.top + "\r\n\r\n" + "\r\n".join(msg.bot[:lines])
    #    return "+OK top of message follows\r\n{}\r\n.".format(text)
    
    def handleRetr(self, data):
        index = int(data.decode('ascii')) - 1
        assert index >= 0
        self.populateMessageIndex()
        msg = self.messages[index]
        content = self.getMessageContent(msg['msgid'])
        if self.debug:
            shared.printLock.acquire()
            sys.stdout.write(str(msg) + ": " + str(content))
            shared.printLock.release()
        yield "+OK {} octets".format(msg['size'])
        yield content['message']
        yield '.'
    
    def handleDele(self, data):
        index = int(data.decode('ascii')) - 1
        assert index >= 0
        self.populateMessageIndex()
        msg = self.messages[index]
        if self.trashMessage(msg['msgid']):
            return ["+OK deleted"]
        else:
            return ["-ERR internal error"]
    
    def handleNoop(self, data):
        return ["+OK"]
    
    def handleQuit(self, data):
        return ["+OK Bitmessage POP3 server signing off"]

class bitmessagePOP3Server(asyncore.dispatcher):
    def __init__(self, debug=False):
        asyncore.dispatcher.__init__(self)
        self.debug = debug

        pop3port = shared.config.getint('bitmessagesettings', 'pop3port')

        self.ssl = shared.config.getboolean('bitmessagesettings', 'pop3ssl')
        if self.ssl:
            self.keyfile = shared.config.get('bitmessagesettings', 'keyfile')
            self.certfile = shared.config.get('bitmessagesettings', 'certfile')

        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.bind(('127.0.0.1', pop3port))
        self.listen(10)

        shared.printLock.acquire()
        print "POP3 server started"
        shared.printLock.release()

    def handle_accept(self):
        sock, peer_address = self.accept()
        if self.ssl:
            sock = ssl.wrap_socket(sock, server_side=True, certfile=self.certfile, keyfile=self.keyfile, ssl_version=ssl.PROTOCOL_SSLv23)
        _ = bitmessagePOP3Connection(sock, peer_address, debug=self.debug)

