import collections
import logging

from telnetlib import LINEMODE, NAWS, NEW_ENVIRON, BINARY, SGA, ECHO, STATUS
from telnetlib import TTYPE, TSPEED, LFLOW, XDISPLOC, IAC, DONT, DO, WONT
from telnetlib import WILL, SE, NOP, TM, DM, BRK, IP, AO, AYT, EC, EL, EOR
from telnetlib import GA, SB, LOGOUT, EXOPL, CHARSET, SNDLOC, theNULL

from slc import SLC_NOSUPPORT, SLC_CANTCHANGE, SLC_VARIABLE, NSLC
from slc import SLC_DEFAULT, SLC_FLUSHOUT, SLC_FLUSHIN, SLC_ACK
from slc import SLC_SYNCH, SLC_BRK, SLC_IP, SLC_AO, SLC_AYT, SLC_EOR
from slc import SLC_ABORT, SLC_EOF, SLC_SUSP, SLC_EC, SLC_EL, SLC_EW
from slc import SLC_RP, SLC_LNEXT, SLC_XON, SLC_XOFF, SLC_FORW1
from slc import SLC_FORW2, SLC_MCL, SLC_MCR, SLC_MCWL, SLC_MCWR, SLC_MCBOL
from slc import SLC_MCEOL, SLC_INSRT, SLC_OVER, SLC_ECR, SLC_EWR, SLC_EBOL
from slc import SLC_EEOL, DEFAULT_SLC_TAB, SLC_nosupport, SLC_definition
from slc import _POSIX_VDISABLE, name_slc_command, Forwardmask

from teldisp import name_unicode

(EOF, SUSP, ABORT, EOR_CMD) = (
        bytes([const]) for const in range(236, 240))
(IS, SEND, INFO) = (bytes([const]) for const in range(3))
(LFLOW_OFF, LFLOW_ON, LFLOW_RESTART_ANY, LFLOW_RESTART_XON) = (
        bytes([const]) for const in range(4))
(LMODE_MODE, LMODE_FORWARDMASK, LMODE_SLC) = (
        bytes([const]) for const in range(1, 4))
(LMODE_MODE_REMOTE, LMODE_MODE_LOCAL, LMODE_MODE_TRAPSIG) = (
        bytes([const]) for const in range(3))
(LMODE_MODE_ACK, LMODE_MODE_SOFT_TAB, LMODE_MODE_LIT_ECHO) = (
    bytes([4]), bytes([8]), bytes([16]))

# see: TelnetStreamReader._default_callbacks
DEFAULT_IAC_CALLBACKS = (
        (BRK, 'brk'), (IP, 'ip'), (AO, 'ao'), (AYT, 'ayt'), (EC, 'ec'),
        (EL, 'el'), (EOF, 'eof'), (SUSP, 'susp'), (ABORT, 'abort'),
        (NOP, 'nop'), (DM, 'dm'), (GA, 'ga'), (EOR_CMD, 'eor'), )
DEFAULT_SLC_CALLBACKS = (
        (SLC_SYNCH, 'dm'), (SLC_BRK, 'brk'), (SLC_IP, 'ip'),
        (SLC_AO, 'ao'), (SLC_AYT, 'ayt'), (SLC_EOR, 'eor'),
        (SLC_ABORT, 'abort'), (SLC_EOF, 'eof'), (SLC_SUSP, 'susp'),
        (SLC_EC, 'ec'), (SLC_EL, 'el'), (SLC_EW, 'ew'), (SLC_RP, 'rp'),
        (SLC_LNEXT, 'lnext'), (SLC_XON, 'xon'), (SLC_XOFF, 'xoff'), )
DEFAULT_EXT_CALLBACKS = (
        (TTYPE, 'ttype'), (TSPEED, 'tspeed'), (XDISPLOC, 'xdisploc'),
        (NEW_ENVIRON, 'env'), (NAWS, 'naws'), (LOGOUT, 'logout'),
        (SNDLOC, 'sndloc',) )

def escape_iac(buf):
    """ .. function:: escape_iac(buf : bytes) -> type(bytes)
        :noindex:

        Return byte buffer with IAC (\xff) escaped.
    """
    assert isinstance(buf, (bytes, bytearray)), buf
    return buf.replace(IAC, IAC + IAC)

class Option(dict):
    def __init__(self, name, log=logging):
        """ .. class:: Option(name : str, log: logging.logger)

            Initialize a Telnet Option database for capturing option
            negotation changes to ``log`` if enabled for debug logging.
        """
        self.name, self.log = name, log
        dict.__init__(self)

    def enabled(self, key):
        """ Returns True of option is enabled."""
        return bool(self.get(key, None) is True)

    def __setitem__(self, key, value):
        if value != dict.get(self, key, None):
            descr = ' + '.join([_name_command(bytes([byte]))
                for byte in key[:2]] + [repr(byte)
                    for byte in key[2:]])
            self.log.debug('{}[{}] = {}'.format(self.name, descr, value))
        dict.__setitem__(self, key, value)
    __setitem__.__doc__ = dict.__setitem__.__doc__


class TelnetStreamReader:
    """
       This class implements a ``feed_byte()`` method that acts as a
       Telnet Is-A-Command (IAC) interpreter. The significance of the
       last byte passed to this method is tested by class instance public
       attributes following the call. A minimal Telnet Service Protocol
       ``data_received`` method should forward each byte, or begin forwarding
       at IAC until  ``is_oob`` tests ``True``, and optionally act on
       functions of ``slc_received``.
   """

    #: a list of system environment variables requested by the server after
    # a client agrees to negotiate NEW_ENVIRON.
    _default_env_request = (
            "USER HOSTNAME UID TERM COLUMNS LINES DISPLAY LANG SYSTEMTYPE "
            "ACCT JOB PRINTER SFUTLNTVER SFUTLNTMODE LC_ALL VISUAL EDITOR "
            "LC_COLLATE LC_CTYPE LC_MESSAGES LC_MONETARY LC_NUMERIC LC_TIME"
            ).split()
    #: Maximum size of sub-negotiation buffer
    SB_MAXSIZE = 2048
    #: Maximum size of Special Linemode Character receive buffer
    SLC_MAXSIZE = 6 * NSLC

    @property
    def is_linemode(self):
        """ If telnet stream appears to be in any linemode, remote or local.
        """
        #   The default Network Terminal is always in linemode, unless
        #   explicitly set False (client sends: WONT, LINEMODE),
        #   or implied by server (server sends: WILL ECHO, WILL SGA).
        if self.is_server:
            return self.remote_option.enabled(LINEMODE) or not (
                    self.local_option.enabled(ECHO) and
                    self.local_option.enabled(SGA))
        # same heuristic is reversed for client point of view (unveried XXX)
        return self.local_option.enabled(LINEMODE) or (
                self.remote_option.enabled(ECHO) and
                self.remote_option.enabled(SGA))

    @property
    def linemode(self):
        """ Linemode instance for stream, or None if stream is in Kludge mode.
        """
        #   A description of the linemode entered may be tested using boolean
        #   instance attributes ``edit``, ``trapsig``, ``soft_tab``, and
        #   ``lit_echo``, or simply its __str__() method.
        return (self._linemode if self.is_linemode else None)

    @property
    def is_server(self):
        """ Telnet stream is used for server end. """
        return bool(self._server)

    @property
    def is_client(self):
        """ Telnet stream is used for client end.
        """
        return bool(not self._server)

    @property
    def is_oob(self):
        """ Last byte processed by ``feed_byte()`` should not be received
            in-band: not duplicated to the client if remote ECHO is enabled,
            and not inserted into an input buffer.
        """
        # Values matching special linemode characters (SLC) are inband.
        # Always True if handled by IAC interpreter and any matching callbacks.
        return (self.iac_received or self.cmd_received)

    def __init__(self, transport, client=False, server=False, log=logging,
            default_slc_tab=DEFAULT_SLC_TAB):
        """
        .. class::TelnetServer(transport, client=False, server=False,
                                log=logging, default_slc_tag=DEFAULT_SLC_TAB)

        Server and Client streams negotiate about capabilities from different
        perspectives, so the mutually exclusive booleans ``client`` and
        ``server`` (default) indicates which end the protocol is attached to.

        Extending or changing protocol capabilities should extend, override,
        or register their own callables, for the local iac, slc, and ext
        callback handlers; mainly those beginning with ``handle``, or by
        registering using the methods beginning with ``set_callback``.
        """
        assert not client == False or not server == False, (
            "Arguments 'client' and 'server' are mutually exclusive")
        self.log = log
        self.transport = transport
        #: total bytes sent to ``feed_byte()``
        self.byte_count = 0
        #: wether flow control enabled by Transmit-Off (XOFF) (defaults
        #  to Ctrl-s), should re-enable Transmit-On (XON) only on receipt
        #  of the XON key (Ctrl-q). Or, when unset, any keypress from client
        #  re-enables transmission (XON).
        self.xon_any = False
        #: set ``True`` if the last byte sent to ``feed_byte()`` is the
        #  beginning of an IAC command (\xff).
        self.iac_received = False
        #: SLC function value if the last byte sent to ``feed_byte()`` is a
        #  matching special line chracter value.
        self.slc_received = False
        #: SLC function values and callbacks are fired for clients in
        #  Kludge mode not otherwise capable of negotiating them, providing
        #  remote editing facilities for dumb clients, such as with ``nc -T``.
        self.slc_simulated = True
        #: IAC command byte value if the last byte sent to ``feed_byte()`` is
        #  part of an IAC command sequence, such as *WILL* or *SB*.
        self.cmd_received = False
        #: True when Flow Control (XON) has been recv until receipt of XOFF.
        self._xmit = True
        #: Sub-negotiation buffer
        self._sb_buffer = collections.deque()
        #: SLC buffer
        self._slc_buffer = collections.deque()
        #: Represents negotiated linemode byte mask if ``is_linemode`` is True.
        self._linemode = Linemode()
        #: True if client acknowledged forwardmask
        self._forwardmask_enabled = False
        #: True if stream is operating in server mode
        self._server = (client in (None, False) or server in (None, True))

        self._init_options()
        self._default_callbacks()
        self._default_slc(default_slc_tab)

    def feed_byte(self, byte):
        """ .. method:: feed_byte(byte : bytes)

            Feed a single byte into Telnet option state machine.
        """
        assert isinstance(byte, (bytes, bytearray)), byte
        assert len(byte) == 1, byte
        self.byte_count += 1
        self._dm_recv = False
        self.slc_received = False
        # list of IAC commands needing 3+ bytes
        iac_mbs = (DO, DONT, WILL, WONT, SB)
        # cmd received is toggled false, unless its a msb.
        self.cmd_received = self.cmd_received in iac_mbs and self.cmd_received

        if byte == IAC:
            self.iac_received = (not self.iac_received)
            if not self.iac_received and self.cmd_received == SB:
                # SB buffer recvs escaped IAC values
                self._sb_buffer.append(IAC)

        elif self.iac_received and not self.cmd_received:
            # parse 2nd byte of IAC, even if recv under SB
            self.cmd_received = cmd = byte
            if cmd not in iac_mbs:
                # DO, DONT, WILL, WONT are 3-byte commands and
                # SB can be of any length. Otherwise, this 2nd byte
                # is the final iac sequence command byte.
                assert cmd in self._iac_callback, _name_command(cmd)
                self._iac_callback[cmd](cmd)
            self.iac_received = False

        elif self.iac_received and self.cmd_received == SB:
            # parse 2nd byte of IAC while while already within
            # IAC SB sub-negotiation buffer, assert command is SE.
            self.cmd_received = cmd = byte
            if cmd != SE:
                self.log.warn('SB buffer interrupted by IAC {}'.format(
                    _name_command(cmd)))
                self._sb_buffer.clear()
            else:
                self.log.debug('recv IAC SE')
                # sub-negotiation end (SE), fire handle_subnegotiation
                try:
                    self.handle_subnegotiation(self._sb_buffer)
                finally:
                    self._sb_buffer.clear()
            self.iac_received = False

        elif self.cmd_received == SB:
            # continue buffering of sub-negotiation command.
            self._sb_buffer.append(byte)
            assert len(self._sb_buffer) < self.SB_MAXSIZE

        elif self.cmd_received:
            # parse 3rd and final byte of IAC DO, DONT, WILL, WONT.
            cmd, opt = self.cmd_received, byte
            self.log.debug('recv IAC {} {}'.format(
                _name_command(cmd), _name_command(opt)))
            if cmd == DO:
                if self.handle_do(opt):
                    self.local_option[opt] = True
                    if self.pending_option.enabled(WILL + opt):
                        self.pending_option[WILL + opt] = False
            elif cmd == DONT:
                self.handle_dont(opt)
                if self.pending_option.enabled(WILL + opt):
                    self.pending_option[WILL + opt] = False
                self.local_option[opt] = False
            elif cmd == WILL:
                if not self.pending_option.enabled(DO + opt) and opt != TM:
                    self.log.debug('WILL {} unsolicited'.format(
                        _name_command(opt)))
                self.handle_will(opt)
                if self.pending_option.enabled(DO + opt):
                    self.pending_option[DO + opt] = False
                if self.pending_option.enabled(DONT + opt):
                    self.pending_option[DONT + opt] = False
            elif cmd == WONT:
                self.handle_wont(opt)
                self.pending_option[DO + opt] = False
            self.iac_received = False
            self.cmd_received = (opt, byte)

        elif self.pending_option.enabled(DO + TM):
            # IAC DO TM was previously sent; discard all input until
            # IAC WILL TM or IAC WONT TM is received by remote end.
            self.log.debug('discarded by timing-mark: {!r}'.format(byte))

        elif (not self.is_linemode and self.slc_simulated  # kludge mode,
                ) or (self.remote_option.enabled(LINEMODE)
                        and self.linemode.remote):  #  remote lm + editing,
            # 'byte' is tested for SLC characters
            (callback, slc_name, slc_def) = self._slc_snoop(byte)
            if slc_name is not None:
                self.log.debug('_slc_snoop({!r}): {}, callback is {}.'.format(
                        byte, name_slc_command(slc_name),
                        callback.__name__ if callback is not None else None))
                if slc_def.flushin:
                    # SLC_FLUSHIN not supported, requires SYNCH (urgent TCP).
                    pass
                if slc_def.flushout:
                    # XXX
                    # We must call transport.pause_writing, create a new send
                    # buffer without incompleted IAC bytes, call
                    # discard_output, write new buffer, then resume_writing
                    pass
                # allow caller to know which SLC function caused linemode
                # to process, even though CR was not yet discovered.
                self.slc_received = slc_name
            if callback is not None:
                callback(slc_name)
        else:
            # standard inband data
            return
        if not self._xmit and self.xon_any and not self.is_oob:
            # any key after XOFF enables XON
            self._slc_callback[SLC_XON]()

    def write(self, data, oob=False):
        """ .. method:: feed_byte(byte : bytes)

            Write data bytes to transport end connected to stream reader.
            Bytes matching IAC (\xff) is escabed by IAC IAC, unless oob=True.
        """
        #   All standard telnet bytes, and bytes within an (IAC SB), (IAC SE)
        #   sub-negotiation buffer must always be escaped.
        #
        #   8-bit ASCII data values greater than 128 cannot be sent inband
        #   unless WILL BINARY ('outbinary') has been agreed, or ``oob``
        #   is ``True``.
        #
        #   If ``oob`` is set ``True``, data is considered
        #   out-of-band and may set high bit.
        assert isinstance(data, (bytes, bytearray)), repr(data)
        if not oob and not self.local_option.enabled(BINARY):
            for pos, byte in enumerate(data):
                assert byte < 128, (
                        'character value {} at pos {} not valid, send '
                        'IAC WILL BINARY first: {}'.format(byte, pos, data))
        self.transport.write(escape_iac(data))

    def send_iac(self, data):
        """ .. method: send_iac(self, data : bytes)

            No transformations of bytes are performed, Only complete
            IAC commands are legal.
        """
        assert isinstance(data, (bytes, bytearray)), data
        assert data and data.startswith(IAC), data
        self.transport.write(data)

    def iac(self, cmd, opt=None):
        """ .. method: iac(self, cmd : bytes, opt : bytes)

            Send Is-A-Command (IAC) 2 or 3-byte command option.

            Returns True if the command was actually sent. Not all commands
            are legal in the context of client, server, or negotiation state,
            emitting a relevant debug warning to the log handler.
        """
        short_iacs = (DM, )
        assert (cmd in (DO, DONT, WILL, WONT)
                or cmd in short_iacs and opt is None), (
                        'Uknown IAC {}.'.format(_name_command(cmd)))
        if opt == LINEMODE:
            if cmd == DO and not self.is_server:
                raise ValueError('DO LINEMODE may only be sent by server.')
            if cmd == WILL and self.is_server:
                raise ValueError('WILL LINEMODE may only be sent by client.')
        if cmd == DO: # XXX any exclusions ?
            if self.remote_option.enabled(opt):
                self.log.debug('skip {} {}; remote_option = True'.format(
                    _name_command(cmd), _name_command(opt)))
                return False
        if cmd in (DO, WILL):
            if self.pending_option.enabled(cmd + opt):
                self.log.debug('skip {} {}; pending_option = True'.format(
                    _name_command(cmd), _name_command(opt)))
                return False
            self.pending_option[cmd + opt] = True
        if cmd == WILL and opt != TM:
            if self.local_option.enabled(opt):
                self.log.debug('skip {} {}; local_option = True'.format(
                    _name_command(cmd), _name_command(opt)))
                return False
        if cmd == DONT and opt not in (LOGOUT,): # XXX any other exclusions?
            if self.remote_option.enabled(opt):
                # warning: some implementations incorrectly reply (DONT, opt),
                # for an option we already said we WONT. This would cause
                # telnet loops for implementations not doing state tracking!
                self.log.debug('skip {} {}; remote_option = True'.format(
                    _name_command(cmd), _name_command(opt)))
            self.remote_option[opt] = False
        elif cmd == WONT:
            self.local_option[opt] = False
        if cmd in short_iacs:
            self.send_iac(IAC + cmd)
        else:
            self.send_iac(IAC + cmd + opt)
        self.log.debug('send IAC {}'.format(_name_command(cmd),
            ' {}'.format(_name_command(opt)) if cmd in short_iacs else ''))

# Public methods for notifying about, soliciting, or advertising state options.
#
    def send_ga(self):
        """ .. method:: send_ga() -> bool

            Send IAC GA (Go-Ahead) only if IAC DONT SGA was received.
            Clients wishing to receive GA should send (DONT SGA). Returns
            True if GA was sent.
        """
        #   Only a few 1970-era hosts require GA (AMES-67, UCLA-CON). The GA
        #   signal is very useful for scripting, such as an 'expect'-like
        #   program flow, or for MUDs, indicating that the last-most received
        #   line is a prompt. Another example of GA is a nethack server
        #   (alt.nethack.org), that indicates to ai bots that it has received
        #   all screen updates.
        #
        if not self.local_option.enabled(SGA):
            self.send_iac(IAC + GA)
            return True



    def request_status(self):
        """ .. method:: request_status() -> bool

            Send STATUS, SEND sub-negotiation, rfc859.
            Returns True if request is valid for telnet state, and was sent.
        """
        #   Does nothing if (WILL, STATUS) has not yet been received,
        #   or an existing SB STATUS SEND request is already pending.
        if not self.remote_option.enabled(STATUS):
            pass
        if not self.pending_option.enabled(SB + STATUS):
            self.pending_option[SB + STATUS] = True
            self.send_iac(
                b''.join([IAC, SB, STATUS, SEND, IAC, SE]))
            # set pending for SB STATUS
            self.pending_option[SB + STATUS] = True
            return True

    def request_tspeed(self):
        """ .. method:: request_tspeed() -> bool

            Send TSPEED, SEND sub-negotiation, rfc1079.
            Returns True if request is valid for telnet state, and was sent.
        """
        #   Does nothing if (WILL, TSPEED) has not yet been received.
        #   or an existing SB TSPEED SEND request is already pending. """
        if not self.remote_option.enabled(TSPEED):
            pass
        if not self.pending_option.enabled(SB + TSPEED):
            self.pending_option[SB + TSPEED] = True
            response = [IAC, SB, TSPEED, SEND, IAC, SE]
            self.log.debug('send: IAC SB TSPEED SEND IAC SE')
            self.send_iac(b''.join(response))
            return True

    def request_charset(self, codepages=None, sep=' '):
        """ .. method:: request_charset(codepages : list, sep : string) -> bool

            Request sub-negotiation CHARSET, rfc 2066.
            Returns True if request is valid for telnet state, and was sent.
        """ # TODO: find client that works!
        #  At least some modern MUD clients and popular asian telnet BBS
        #  systems use CHARSET, and reply 'UTF-8' (or 'GBK',).  """
        if not self.remote_option.enabled(CHARSET):
            pass
        (REQUEST, ACCEPTED, REJECTED, TTABLE_IS, TTABLE_REJECTED,
            TTABLE_ACK, TTABLE_NAK) = (bytes([const]) for const in range(1, 8))
        if not self.pending_option.enabled(SB + CHARSET):
            self.pending_option[SB + CHARSET] = True
            response = [IAC, SB, CHARSET, REQUEST]
            response.extend(bytes(sep.join(codepages), 'ascii'))
            response.extend([IAC, SE])
            self.log.debug('send: IAC SB CHARSET REQUEST {} IAC SE'.format(
                sep.join(codepages)))
            self.send_iac(b''.join(response))
            return True


    def request_env(self, env=None):
        """ .. method:: request_env(env : list) -> bool

            Request sub-negotiation NEW_ENVIRON, rfc 1572.
            Returns True if request is valid for telnet state, and was sent.

            ``env`` is list ascii uppercase keys of values requested. Default
            value is when unset is instance attribute ``_default_env_request``.
            Returns True if request is valid for telnet state, and was sent.
        """
        # May only be requested by the server end. Sends IAC SB ``kind``
        # SEND IS sub-negotiation, rfc1086, using list of ascii string
        # values ``self._default_env_request``, which is mostly variables
        # for impl.-specific extensions, such as TERM type, or USER for auth.
        request_ENV = self._default_env_request if env is None else env
        assert self.is_server
        kind = NEW_ENVIRON
        if not self.remote_option.enabled(kind):
            self.log.debug('cannot send SB {} SEND IS '
                'without receipt of WILL {}'.format(
                    _name_command(kind), _name_command(kind)))
            return False
        if self.pending_option.enabled(SB + kind + SEND + IS):
            self.log.debug('cannot send SB {} SEND IS, '
                'request pending.'.format(_name_command(kind)))
            return False
        self.pending_option[SB + kind + SEND + IS] = True
        response = collections.deque()
        response.extend([IAC, SB, kind, SEND, IS])
        for idx, env in enumerate(request_ENV):
            response.extend([bytes(char, 'ascii') for char in env])
            if idx < len(request_ENV) - 1:
                response.append(theNULL)
        response.extend([b'\x03', IAC, SE])
        self.log.debug('send: {!r}'.format(b''.join(response)))
        self.send_iac(b''.join(response))
        return True

    def request_xdisploc(self):
        """ .. method:: request_xdisploc() -> bool

            Send XDISPLOC, SEND sub-negotiation, rfc1086.
            Returns True if request is valid for telnet state, and was sent.
        """
        if not self.remote_option.enabled(XDISPLOC):
            pass
        if not self.pending_option.enabled(SB + XDISPLOC):
            self.pending_option[SB + XDISPLOC] = True
            response = [IAC, SB, XDISPLOC, SEND, IAC, SE]
            self.log.debug('send: IAC SB XDISPLOC SEND IAC SE')
            self.send_iac(b''.join(response))
            return True

    def request_ttype(self):
        """ .. method:: request_ttype() -> bool

            Send TTYPE SEND sub-negotiation, rfc930.
            Returns True if request is valid for telnet state, and was sent.
        """
        if not self.remote_option.enabled(TTYPE):
            pass
        if not self.pending_option.enabled(SB + TTYPE):
            self.pending_option[SB + TTYPE] = True
            response = [IAC, SB, TTYPE, SEND, IAC, SE]
            self.log.debug('send: IAC SB TTYPE SEND IAC SE')
            self.send_iac(b''.join(response))
            return True

    def send_eor(self):
        """ .. method:: request_eor() -> bool

            Send IAC EOR_CMD (End-of-Record) only if IAC DO EOR was received.
            Returns True if request is valid for telnet state, and was sent.
        """
        if not self.local_option.enabled(EOR):
            self.send_iac(IAC + EOR_CMD)

    def send_lineflow_mode(self):
        """ .. method send_lineflow_mod() -> bool

        Send LFLOW mode sub-negotiation, rfc1372.
        """
        if not self.remote_option.enabled(LFLOW):
            return
        mode = LFLOW_RESTART_ANY if self.xon_any else LFLOW_RESTART_XON
        desc = 'LFLOW_RESTART_ANY' if self.xon_any else 'LFLOW_RESTART_XON'
        self.send_iac(b''.join([IAC, SB, LFLOW, mode, IAC, SE]))
        self.log.debug('send: IAC SB LFLOW %s IAC SE', desc)

    def send_linemode(self, linemode=None):
        """ Request the client switch to linemode ``linemode``, an
        of the Linemode class, or self._linemode by default.
        """
        assert self.is_server, (
                'SB LINEMODE LMODE_MODE cannot be sent by client')
        assert self.remote_option.enabled(LINEMODE), (
                'SB LINEMODE LMODE_MODE cannot be sent; '
                'WILL LINEMODE not received.')
        if linemode is not None:
            self.log.debug('Linemode is %s', linemode)
            self._linemode = linemode
        self.send_iac(IAC + SB + LINEMODE
                    + LMODE_MODE + self._linemode.mask
                    + IAC + SE)
        self.log.debug('sent IAC SB LINEMODE MODE %s IAC SE', self._linemode)

    def request_forwardmask(self, fmask=None):
        """ Request the client forward the control characters indicated
            in the Forwardmask class instance ``fmask``. When fmask is
            None, a forwardmask is generated for the SLC characters registered
            in the SLC tab, ``_slctab``.
        """
        assert self.is_server, (
                'DO FORWARDMASK may only be sent by server end')
        assert self.remote_option.enabled(LINEMODE), (
                'cannot send DO FORWARDMASK without receipt of WILL LINEMODE.')
        if fmask is None:
            fmask = self._generate_forwardmask()
        assert isinstance(fmask, Forwardmask), fmask
        sb_cmd = LINEMODE + DO + LMODE_FORWARDMASK + escape_iac(fmask.value)
        self.log.debug('send IAC SB LINEMODE DO LMODE_FORWARDMASK::')
        for maskbit_descr in fmask.__repr__():
            self.log.debug('  %s', maskbit_descr)
        self.send_iac(IAC + SB + sb_cmd + IAC + SE)
        self.pending_option[SB + LINEMODE] = True

# Public is-a-command (IAC) callbacks
#
    def set_iac_callback(self, cmd, func):
        """ Register callable ``func`` as callback for IAC ``cmd``.

            BRK, IP, AO, AYT, EC, EL, EOR_CMD, EOF, SUSP, ABORT, and NOP.

            These callbacks receive a single argument, the IAC ``cmd`` which
            triggered it.
        """
        assert callable(func), ('Argument func must be callable')
        assert cmd in (BRK, IP, AO, AYT, EC, EL, EOR_CMD, EOF, SUSP,
                       ABORT, NOP, DM, GA), cmd
        self._iac_callback[cmd] = func

    def handle_nop(self, cmd):
        """ XXX Handle IAC No-Operation (NOP)
        """
        self.log.debug('IAC NOP: Null Operation')

    def handle_ga(self, cmd):
        """ XXX Handle IAC Go-Ahead (GA)
        """
        self.log.debug('IAC GA: Go-Ahead')

    def handle_dm(self, cmd):
        """ XXX Handle IAC Data-Mark (DM)

            Callback sets ``self._dm_recv``.  when IAC + DM is received.
            The TCP transport is not tested for OOB/TCP Urgent, so an old
            teletype half-duplex terminal may inadvertantly send unintended
            control sequences up until now,

            Oh well.  """
        self.log.debug('IAC DM: received')
        #: ``True`` if the last byte sent to ``feed_byte()`` was the end
        #  of an *IAC DM* has been received. MSG_OOB not implemented, so
        #  this mechanism _should not be implmeneted_.
        self._dm_recv = True
        #self.iac(DM)

# Public mixed-mode SLC and IAC callbacks
#
    def handle_el(self, byte):
        """ XXX Handle IAC Erase Line (EL) or SLC_EL.

            Provides a function which discards all the data ready on current
            line of input. The prompt should be re-displayed.
        """
        self.log.debug('IAC EL: Erase Line')

    def handle_eor(self, byte):
        """ XXX Handle IAC End of Record (EOR_CMD) or SLC_EOR.
        """
        self.log.debug('IAC EOR_CMD: End of Record')

    def handle_abort(self, byte):
        """ XXX Handle IAC Abort (ABORT) rfc1184, or SLC_ABORT.

            Similar to Interrupt Process (IP), but means only to abort or
            terminate the process to which the NVT is connected.
        """
        self.log.debug('IAC ABORT: Abort')

    def handle_eof(self, byte):
        """ XXX Handle End of Record (IAC, EOF), rfc1184 or SLC_EOF.
        """
        self.log.debug('IAC EOF: End of File')

    def handle_susp(self, byte):
        """ XXX Handle Suspend Process (SUSP), rfc1184 or SLC_SUSP.

            Suspends the execution of the current process attached to the NVT
            in such a way that another process will take over control of the
            NVT, and the suspended process can be resumed at a later time.
        """
        # If the receiving system does not support this functionality, it
        # should be ignored.
        self.log.debug('IAC SUSP: Suspend')

    def handle_brk(self, byte):
        """ XXX Handle IAC Break (BRK) or SLC_BRK (Break).

            Sent by clients to indicate BREAK keypress. This is not the same
            as IP (^c), but a means to map sysystem-dependent break key such
            as found on an IBM Systems.
        """
        self.log.debug('IAC BRK: Break')

    def handle_ayt(self, byte):
        """ XXX Handle IAC Are You There (AYT) or SLC_AYT.

            Provides the user with some visible (e.g., printable) evidence
            that the system is still up and running.
        """
        #   Terminal servers that respond to AYT usually print the status
        #   of the client terminal session, its speed, type, and options.
        self.log.debug('IAC AYT: Are You There?')

    def handle_ip(self, byte):
        """ XXX Handle IAC Interrupt Process (IP) or SLC_IP
        """
        self.log.debug('IAC IP: Interrupt Process')

    def handle_ao(self, byte):
        """ XXX Handle IAC Abort Output (AO) or SLC_AO.

            Discards any remaining output on the transport buffer.
        """
        #   "If the AO were received [...] a reasonable implementation would
        #   be to suppress the remainder of the text string, *but transmit the
        #   prompt character and the preceding <CR><LF>*."
        # XXX TODO: Must netsend()
        self.log.debug('IAC AO: Abort Output')
        pass
        #self.stream.discard_output()

    def handle_xon(self, byte):
        """ XXX handle Transmit-On (IAC, XON) or SLC_XON.

            Pauses writing to the transport.
        """
        self.log.debug('IAC XON: Transmit On')
        self._xmit = True
        self.transport.resume_writing()

    def handle_ec(self, byte):
        """ XXX Handle IAC + SLC or SLC_EC (Erase Character).

            Provides a function which deletes the last preceding undeleted
            character from data ready on current line of input.
        """
        self.log.debug('IAC EC: Erase Character')

# public Special Line Mode (SLC) callbacks
#
    def set_slc_callback(self, slc, func):
        """ Register ``func`` as callbable for receipt of SLC character
            negotiated for the SLC command ``slc`` in  ``_slc_callback``,
            keyed by ``slc`` and valued by its handling function.

            SLC_SYNCH, SLC_BRK, SLC_IP, SLC_AO, SLC_AYT, SLC_EOR, SLC_ABORT,
            SLC_EOF, SLC_SUSP, SLC_EC, SLC_EL, SLC_EW, SLC_RP, SLC_XON,
            SLC_XOFF, (...)

            These callbacks receive a single argument: the SLC function
            byte that fired it. Some SLC and IAC functions are intermixed;
            which signalling mechanism used by client can be tested by
            evaulating this argument.
            """
        assert callable(func), ('Argument func must be callable')
        assert (type(slc) == bytes and
                0 < ord(slc) < NSLC + 1), ('Uknown SLC byte: %r' % (slc,))
        self._slc_callback[slc] = func

    def handle_ew(self, slc):
        """ XXX Handle SLC_EW (Erase Word).

            Provides a function which deletes the last preceding undeleted
            character, and any subsequent bytes until next whitespace character
            from data ready on current line of input.
        """
        self.log.debug('IAC EC: Erase Word')

    def handle_rp(self, slc):
        """ Handle SLC Repaint.
        """ # XXX
        self.log.debug('SLC RP: Repaint')

    def handle_lnext(self, slc):
        """ Handle SLC LINE NEXT?
        """ # XXX
        self.log.debug('IAC LNEXT: Line Next')

    def handle_xoff(self, slc):
        """ Called when SLC_XOFF is received.
        """
        self.log.debug('IAC XOFF: Transmit Off')
        self._xmit = False
        self.transport.pause_writing()

# public Telnet extension callbacks
#
    def set_ext_callback(self, cmd, func):
        """ Register ``func`` as callback for subnegotiation result of ``cmd``.

        cmd must be one of: TTYPE, TSPEED, XDISPLOC, NEW_ENVIRON, or NAWS.

        These callbacks may receive a number of arguments.

        Callbacks for ``TTYPE`` and ``XDISPLOC`` receive a single argument
        as a bytestring. ``NEW_ENVIRON`` receives a single argument as
        dictionary. ``NAWS`` receives two integer arguments (width, height),
        and ``TSPEED`` receives two integer arguments (rx, tx).
        """
        assert cmd in (TTYPE, TSPEED, XDISPLOC,
                NEW_ENVIRON, NAWS, LOGOUT, CHARSET, SNDLOC), cmd
        assert callable(func), ('Argument func must be callable')
        self._ext_callback[cmd] = func

    def handle_xdisploc(self, xdisploc):
        """ XXX Receive XDISPLAY value ``xdisploc``, rfc1096.

            xdisploc string format is '<host>:<dispnum>[.<screennum>]'.
        """
        self.log.debug('X Display is {}'.format(xdisploc))

    def handle_sndloc(self, location):
        """ XXX Receive LOCATION value ``location``, rfc779.
        """
        self.log.debug('Location is {}'.format(location))

    def handle_ttype(self, ttype):
        """ XXX Receive TTYPE value ``ttype``, rfc1091.

            Often value of TERM, or analogous to client's emulation capability,
            common values for non-posix client replies are 'VT100', 'VT102',
            'ANSI', 'ANSI-BBS', or even a mud client identifier. RFC allows
            subsequent requests, the client may solicit multiple times, and
            the client indicates 'end of list' by cycling the return value.

            Some example values: VT220, VT100, IBM-3278-(2 through 5),
                ANSITERM, ANSI, TTY, and 5250.
        """
        self.log.debug('Terminal type is %r', ttype)

    def handle_naws(self, width, height):
        """ XXX Receive window size ``width`` and ``height``, rfc1073
        """
        self.log.debug('Terminal cols=%d, rows=%d', width, height)

    def handle_env(self, env):
        """ XXX Receive environment variables as dict, rfc1572
            negotiation, as dictionary.
        """
        self.log.debug('env=%r', env)

    def handle_tspeed(self, rx, tx):
        """ XXX Receive terminal speed from TSPEED as int, rfc1079
        """
        self.log.debug('Terminal Speed rx:%d, tx:%d', rx, tx)


    def handle_location(self, location):
        """ XXX Handle (IAC, SB, SNDLOC, <location>, IAC, SE), RFC 779.
        """
        self.log.debug('Terminal Location:%s', location)

    def handle_logout(self, cmd):
        """ XXX Handle (IAC, (DO | DONT | WILL | WONT), LOGOUT), RFC 727.

            Only the server end may receive (DO, DONT).
            Only the client end may receive (WILL, WONT).
            """
        # Close the transport on receipt of DO, Reply DONT on receipt
        # of WILL.  Nothing is done on receipt of DONT or WONT LOGOFF.
        if cmd == DO:
            self.log.info('client requests DO LOGOUT')
            self.transport.close()
        elif cmd == DONT:
            self.log.info('client requests DONT LOGOUT')
        elif cmd == WILL:
            self.log.debug('recv WILL TIMEOUT (timeout warning)')
            self.log.debug('send IAC DONT LOGOUT')
            self.iac(DONT, LOGOUT)
        elif cmd == WONT:
            self.log.info('recv IAC WONT LOGOUT (server refuses logout')

# public derivable methods DO, DONT, WILL, and WONT negotiation
#
    def handle_do(self, opt):
        """ XXX Process byte 3 of series (IAC, DO, opt) received by remote end.

        This method can be derived to change or extend protocol capabilities.
        The result of a supported capability is a response of (IAC, WILL, opt)
        and the setting of ``self.local_option[opt]`` of ``True``.
        """
        # For unsupported capabilities, RFC specifies a response of
        # (IAC, WONT, opt).  Similarly, set ``self.local_option[opt]``
        # to ``False``.
        #
        # This method returns True if the opt enables the willingness of the
        # remote end to accept a telnet capability, such as NAWS. It returns
        # False for unsupported option, or an option invalid in that context,
        # such as LOGOUT.
        self.log.debug('handle_do(%s)' % (_name_command(opt)))
        if opt == ECHO and not self.is_server:
            self.log.warn('cannot recv DO ECHO on client end.')
        elif opt == LINEMODE and self.is_server:
            self.log.warn('cannot recv DO LINEMODE on server end.')
        elif opt == LOGOUT and self.is_server:
            self.log.warn('cannot recv DO LOGOUT on client end')
        elif opt == TM:
            self.iac(WILL, TM)
        elif opt == LOGOUT:
            self._ext_callback[LOGOUT](DO)
        elif opt in (ECHO, LINEMODE, BINARY, SGA, LFLOW, EXOPL, EOR):
            if not self.local_option.enabled(opt):
                self.iac(WILL, opt)
            return True
        elif opt == STATUS:
            if not self.local_option.enabled(opt):
                self.iac(WILL, STATUS)
            self._send_status()
            return True
        else:
            if self.local_option.get(opt, None) is None:
                self.iac(WONT, opt)
            self.log.warn('Unhandled: DO %s.' % (_name_command(opt),))
        return False

    def handle_dont(self, opt):
        """ Process byte 3 of series (IAC, DONT, opt) received by remote end.

        This only results in ``self.local_option[opt]`` set to ``False``, with
        the exception of (IAC, DONT, LOGOUT), which only signals a callback
        to ``handle_logout(DONT)``.
        """
        self.log.debug('handle_dont(%s)' % (_name_command(opt)))
        if opt == LOGOUT:
            assert self.is_server, ('cannot recv DONT LOGOUT on server end')
            self._ext_callback[LOGOUT](DONT)
            return
        # many implementations (wrongly!) sent a WONT in reply to DONT. It
        # sounds reasonable, but it can and will cause telnet loops. (ruby?)
        # Correctly, a DONT can not be declined, so there is no need to
        # affirm in the negative.
        self.local_option[opt] = False

    def handle_will(self, opt):
        """ Process byte 3 of series (IAC, DONT, opt) received by remote end.

        The remote end requests we perform any number of capabilities. Most
        implementations require an answer in the affirmative with DO, unless
        DO has meaning specific for only client or server end, and
        dissenting with DONT.

        WILL ECHO is only legally received _for clients_, answered with DO.
        WILL NAWS is only legally received _for servers_, answered with DO.
        BINARY and SGA are answered with DO.  STATUS, NEW_ENVIRON, XDISPLOC,
        and TTYPE is answered with sub-negotiation SEND. The env variables
        requested in response to WILL NEW_ENVIRON is specified by list
        ``self._default_env_request``. All others are replied with DONT.

        The result of a supported capability is a response of (IAC, DO, opt)
        and the setting of ``self.remote_option[opt]`` of ``True``. For
        unsupported capabilities, RFC specifies a response of (IAC, DONT, opt).
        Similarly, set ``self.remote_option[opt]`` to ``False``.  """
        self.log.debug('handle_will(%s)' % (_name_command(opt)))
        if opt in (BINARY, SGA, ECHO, NAWS, LINEMODE, EOR, SNDLOC):
            if opt == ECHO and self.is_server:
                raise ValueError('cannot recv WILL ECHO on server end')
            if opt in (NAWS, LINEMODE, SNDLOC) and not self.is_server:
                raise ValueError('cannot recv WILL %s on client end' % (
                    _name_command(opt),))
            if not self.remote_option.enabled(opt):
                self.remote_option[opt] = True
                self.iac(DO, opt)
            if opt in (NAWS, LINEMODE, SNDLOC):
                self.pending_option[SB + opt] = True
                if opt == LINEMODE:
                    # server sets the initial mode and sends forwardmask,
                    self.send_linemode(self._default_linemode)
        elif opt == TM:
            if opt == TM and not self.pending_option.enabled(DO + TM):
                raise ValueError('cannot recv WILL TM, must first send DO TM.')
            self.log.debug('WILL TIMING-MARK')
            self.pending_option[DO + TM] = False
        elif opt == LOGOUT:
            if opt == LOGOUT and not self.is_server:
                raise ValueError('cannot recv WILL LOGOUT on server end')
            self._ext_callback[LOGOUT](WILL)
        elif opt == STATUS:
            self.remote_option[opt] = True
            self.request_status()
        elif opt == LFLOW:
            if opt == LFLOW and not self.is_server:
                raise ValueError('WILL LFLOW not supported on client end')
            self.remote_option[opt] = True
            self.send_lineflow_mode()
        elif opt == NEW_ENVIRON:
            self.remote_option[opt] = True
            self.request_env()
        elif opt == CHARSET:
            self.remote_option[opt] = True
            self.request_charset()
        elif opt == XDISPLOC:
            if opt == XDISPLOC and not self.is_server:
                raise ValueError('cannot recv WILL XDISPLOC on client end')
            self.remote_option[opt] = True
            self.request_xdisploc()
        elif opt == TTYPE:
            if opt == TTYPE and not self.is_server:
                raise ValueError('cannot recv WILL TTYPE on client end')
            self.remote_option[opt] = True
            self.request_ttype()
        elif opt == TSPEED:
            self.remote_option[opt] = True
            self.request_tspeed()
        else:
            self.remote_option[opt] = False
            self.iac(DONT, opt)
            raise ValueError('Unhandled: WILL %s.' % (_name_command(opt),))

    def handle_wont(self, opt):
        """ Process byte 3 of series (IAC, WONT, opt) received by remote end.

        (IAC, WONT, opt) is a negative acknolwedgement of (IAC, DO, opt) sent.

        The remote end requests we do not perform a telnet capability.

        It is not possible to decline a WONT. ``T.remote_option[opt]`` is set
        False to indicate the remote end's refusal to perform ``opt``.
        """
        self.log.debug('handle_wont(%s)' % (_name_command(opt)))
        if opt == TM and not self.pending_option.enabled(DO + TM):
            raise ValueError('WONT TM received but DO TM was not sent')
        elif opt == TM:
            self.log.debug('WONT TIMING-MARK')
            self.pending_option[DO + TM] = False
        elif opt == LOGOUT:
            assert not (self.is_server), (
                'cannot recv WONT LOGOUT on server end')
            if not self.pending_option(DO + LOGOUT):
                self.log.warn('Server sent WONT LOGOUT unsolicited')
            self._ext_callback[LOGOUT](WONT)
        else:
            self.remote_option[opt] = False

# public derivable Sub-Negotation parsing
#
    def handle_subnegotiation(self, buf):
        """ Callback for end of sub-negotiation buffer.

            SB options handled here are TTYPE, XDISPLOC, NEW_ENVIRON,
            NAWS, and STATUS, and are delegated to their ``handle_``
            equivalent methods. Implementors of additional SB options
            should extend this method.
        """
        #   Changes to the default responses should replace the
        #   default callbacks ``handle_ttype``, ``handle_xdisploc``,
        #   ``handle_env``, and ``handle_naws``, by using
        #   ``set_ext_callback(opt_byte, func)``.
        #
        assert buf, ('SE: buffer empty')
        assert buf[0] != theNULL, ('SE: buffer is NUL')
        assert len(buf) > 1, ('SE: buffer too short: %r' % (buf,))
        cmd = buf[0]
        if self.is_server:
            assert cmd in (LINEMODE, LFLOW, NAWS, SNDLOC,
                NEW_ENVIRON, TTYPE, TSPEED, XDISPLOC, STATUS), _name_command(cmd)
        if self.pending_option.enabled(SB + cmd):
            self.pending_option[SB + cmd] = False
        else:
            self.log.debug('[SB + %s] unsolicited', _name_command(cmd))
        if cmd == LINEMODE: self._handle_sb_linemode(buf)
        elif cmd == LFLOW:
            self._handle_sb_lflow(buf)
        elif cmd == NAWS:
            self._handle_sb_naws(buf)
        elif cmd == SNDLOC:
            self._handle_sb_sndloc(buf)
        elif cmd == NEW_ENVIRON:
            self._handle_sb_env(buf)
        elif (cmd, buf[1]) == (TTYPE, IS):
            self._handle_sb_ttype(buf)
        elif (cmd, buf[1]) == (TSPEED, IS):
            self._handle_sb_tspeed(buf)
        elif (cmd, buf[1]) == (XDISPLOC, IS):
            self._handle_sb_xdisploc(buf)
        elif (cmd, buf[1]) == (STATUS, SEND):
            self._send_status()
        else:
            raise ValueError('SE: unhandled: %r' % (buf,))

# LINEMODE and SLC-related public methods
#
    def set_default_linemode(self, lmode=None):
        """ Set the initial line mode requested by the server if client
            supports LINEMODE negotiation. The default is::
                Linemode(bytes(
                    ord(LMODE_MODE_REMOTE) | ord(LMODE_MODE_LIT_ECHO)))
            which indicates remote editing, and control characters (\b)
            are displayed to the client terminal without transposing,
            such that \b is written to the client screen, and not '^G'.
        """
        assert lmode is None or isinstance(lmode, Linemode), lmode
        if lmode is None:
            self._default_linemode = Linemode(bytes([
                    ord(LMODE_MODE_REMOTE) | ord(LMODE_MODE_LIT_ECHO)]))
        else:
            self._default_linemode = lmode

# Private sub-negotiation (SB) routines
#
    def _handle_sb_tspeed(self, buf):
        assert buf.popleft() == TSPEED
        assert buf.popleft() == IS
        rx, tx = str(), str()
        while len(buf):
            value = buf.popleft()
            if value == b',':
                break
            rx += value.decode('ascii')
        while len(buf):
            value = buf.popleft()
            if value == b',':
                break
            tx += value.decode('ascii')
        self.log.debug('sb_tspeed: %s, %s', rx, tx)
        self._ext_callback[TSPEED](int(rx), int(tx))

    def _handle_sb_xdisploc(self, buf):
        assert buf.popleft() == XDISPLOC
        assert buf.popleft() == IS
        xdisploc_str = b''.join(buf).decode('ascii')
        self.log.debug('sb_xdisploc: %s', xdisploc_str)
        self._ext_callback[XDISPLOC](xdisploc_str)

    def _handle_sb_ttype(self, buf):
        assert buf.popleft() == TTYPE
        assert buf.popleft() == IS
        ttype_str = b''.join(buf).decode('ascii')
        self.log.debug('sb_ttype: %s', ttype_str)
        self._ext_callback[TTYPE](ttype_str)

    def _handle_sb_env(self, buf):
        assert len(buf) > 2, ('SE: buffer too short: %r' % (buf,))
        kind = buf.popleft()
        opt = buf.popleft()
        assert opt in (IS, INFO, SEND), opt
        assert kind == NEW_ENVIRON
        if opt == SEND:
            self._handle_sb_env_send(buf)
        if opt in (IS, INFO):
            assert self.is_server, ('SE: cannot recv from server: %s %s' % (
                _name_command(kind), 'IS' if opt == IS else 'INFO',))
            if opt == IS:
                if not self.pending_option.enabled(SB + kind + SEND + IS):
                    self.log.debug('%s IS unsolicited', _name_command(opt))
                self.pending_option[SB + kind + SEND + IS] = False
            if self.pending_option.get(SB + kind + SEND + IS, None) is False:
                # a pending option of value of 'False' means it previously
                # completed, subsequent environment values should have been
                # send as INFO ..
                self.log.debug('%s IS already recv; expected INFO.',
                        _name_command(kind))
            breaks = list([idx for (idx, byte) in enumerate(buf)
                           if byte in (theNULL, b'\x03')])
            env = {}
            for start, end in zip(breaks, breaks[1:]):
                # not the best looking code, how do we splice & split bytes ..?
                decoded = bytes([ord(byte) for byte in buf]).decode('ascii')
                pair = decoded[start + 1:end].split('\x01', 1)
                if 2 == len(pair):
                    key, value = pair
                    env[key] = value
            self.log.debug('sb_env %s: %r', _name_command(opt), env)
            self._ext_callback[kind](env)
            return

    def _handle_sb_env_send(self, buf):
        raise NotImplementedError  # recv by client

    def _handle_sb_sndloc(self, buf):
        location_str = b''.join(buf).decode('ascii')
        self._ext_callback[SNDLOC](location_str)

    def _handle_sb_naws(self, buf):
        assert buf.popleft() == NAWS
        columns = str((256 * ord(buf[0])) + ord(buf[1]))
        rows = str((256 * ord(buf[2])) + ord(buf[3]))
        self.log.debug('sb_naws: %s, %s', int(columns), int(rows))
        self._ext_callback[NAWS](int(columns), int(rows))

    def _handle_sb_lflow(self, buf):
        """ Handle receipt of (IAC, SB, LFLOW).
        """ # XXX
        assert buf.popleft() == LFLOW
        assert self.local_option.enabled(LFLOW), (
            'received IAC SB LFLOW wihout IAC DO LFLOW')
        self.log.debug('sb_lflow: %r', buf)


    def _handle_sb_linemode(self, buf):
        assert buf.popleft() == LINEMODE
        cmd = buf.popleft()
        if cmd == LMODE_MODE:
            self._handle_sb_linemode_mode(buf)
        elif cmd == LMODE_SLC:
            self._handle_sb_linemode_slc(buf)
        elif cmd in (DO, DONT, WILL, WONT):
            opt = buf.popleft()
            self.log.debug('recv SB LINEMODE %s FORWARDMASK%s.',
                    _name_command(cmd), '(...)' if len(buf) else '')
            assert opt == LMODE_FORWARDMASK, (
                    'Illegal byte follows IAC SB LINEMODE %s: %r, '
                    ' expected LMODE_FORWARDMASK.' (_name_command(cmd), opt))
            self._handle_sb_forwardmask(cmd, buf)
        else:
            raise ValueError('Illegal IAC SB LINEMODE command, %r' % (
                _name_command(cmd),))

    def _handle_sb_linemode_mode(self, buf):
        assert len(buf) == 1
        self._linemode = Linemode(buf[0])
        self.log.debug('Linemode MODE is %s.' % (self.linemode,))

    def _handle_sb_linemode_slc(self, buf):
        """ Process and reply to linemode slc command function triplets. """
        assert 0 == len(buf) % 3, ('SLC buffer must be byte triplets')
        self._slc_start()
        while len(buf):
            func = buf.popleft()
            flag = buf.popleft()
            value = buf.popleft()
            self._slc_process(func, SLC_definition(flag, value))
        self._slc_end()
        self.request_forwardmask()

    def _handle_sb_forwardmask(self, cmd, buf):
        # set and report about pending options by 2-byte opt,
        if self.is_server:
            assert self.remote_option.enabled(LINEMODE), (
                    'cannot recv LMODE_FORWARDMASK %s (%r) '
                    'without first sending DO LINEMODE.' % (cmd, buf,))
            assert cmd not in (DO, DONT), (
                    'cannot recv %s LMODE_FORWARDMASK on server end',
                    _name_command(cmd,))
        if self.is_client:
            assert self.local_option.enabled(LINEMODE), (
                    'cannot recv %s LMODE_FORWARDMASK without first '
                    ' sending WILL LINEMODE.')
            assert cmd not in (WILL, WONT), (
                    'cannot recv %s LMODE_FORWARDMASK on client end',
                    _name_command(cmd,))
            assert cmd not in (DONT) or len(buf) == 0, (
                    'Illegal bytes follow DONT LMODE_FORWARDMASK: %r' % (
                        buf,))
            assert cmd not in (DO) and len(buf), (
                    'bytes must follow DO LMODE_FORWARDMASK')
        if cmd in (WILL, WONT):
            self._forwardmask_enabled = cmd is WILL
        elif cmd in (DO, DONT):
            self._forwardmask_enabled = cmd is DO
            if cmd == DO:
                self._handle_do_forwardmask(buf)

    def _handle_do_forwardmask(self, buf):
        """ Handles buffer received in SB LINEMODE DO FORWARDMASK <buf>
        """ # XXX UNIMPLEMENTED: ( received on client )
        pass

    def _send_status(self):
        """ Respond after DO STATUS received by client (rfc859). """
        assert (self.pending_option.enabled(WILL + STATUS)
                or self.local_option.enabled(STATUS)), (u'Only the sender '
                'of IAC WILL STATUS may send IAC SB STATUS IS.')
        response = collections.deque()
        response.extend([IAC, SB, STATUS, IS])
        for opt, status in self.local_option.items():
            # status is 'WILL' for local option states that are True,
            # and 'WONT' for options that are False.
            response.extend([WILL if status else WONT, opt])
        for opt, status in self.remote_option.items():
            # status is 'DO' for remote option states that are True,
            # or for any DO option requests pending reply. status is
            # 'DONT' for any remote option states that are False,
            # or for any DONT option requests pending reply.
            if status or DO + opt in self.pending_option:
                response.extend([DO, opt])
            elif not status or DONT + opt in self.pending_option:
                response.extend([DONT, opt])
        response.extend([IAC, SE])
        self.log.debug('send: %s', ', '.join([
            _name_command(byte) for byte in response]))
        self.send_iac(bytes([ord(byte) for byte in response]))
        if self.pending_option.enabled(WILL + STATUS):
            self.pending_option[WILL + STATUS] = False

# private Special Linemode Character (SLC) routines
#

    def _default_slc(self, tabset):
        """ Set property ``_slctab`` to default SLC tabset, unless it
            is unlisted (as is the case for SLC_MCL+), then set as
            SLC_NOSUPPORT _POSIX_VDISABLE (0xff).

            ``_slctab`` is a dictionary of SLC functions, such as SLC_IP,
            to a tuple of the handling character and support level.
        """
        self._slctab = {}
        self._default_tabset = tabset
        for slc in range(NSLC + 1):
            self._slctab[bytes([slc])] = tabset.get(bytes([slc]),
                    SLC_definition(SLC_NOSUPPORT, _POSIX_VDISABLE))

    def _slc_snoop(self, byte):
        """ Scan ``self._slctab`` for matching byte values.

            If any are discovered, the (callback, func_byte, slc_definition)
            is returned. Otherwise (None, None, None) is returned.
        """
        # scan byte for SLC function mappings, if any, return function
        for slc_func, slc_def in self._slctab.items():
            if byte == slc_def.val and slc_def.val != theNULL:
                callback = self._slc_callback.get(slc_func, None)
                return (callback, slc_func, slc_def)
        return (None, None, None)


    def _slc_end(self):
        """ Send any SLC pending SLC changes sotred in _slc_buffer """
        if 0 == len(self._slc_buffer):
            self.log.debug('slc_end: IAC SE')
        else:
            self.write(b''.join(self._slc_buffer), oob=True)
            self.log.debug('slc_end: (%r) IAC SE', b''.join(self._slc_buffer))
        self.send_iac(IAC + SE)
        self._slc_buffer.clear()

    def _slc_start(self):
        """ Send IAC SB LINEMODE SLC header """
        self.send_iac(IAC + SB + LINEMODE + LMODE_SLC)
        self.log.debug('slc_start: IAC + SB + LINEMODE + SLC')

    def _slc_send(self):
        """ Send all special characters that are supported """
        send_count = 0
        for func in range(NSLC + 1):
            if self._slctab[bytes([func])].nosupport:
                continue
            if func is 0 and not self.is_server:
                # only the server may send an octet with the first
                # byte (func) set as 0 (SLC_NOSUPPORT).
                continue
            self._slc_add(bytes([func]))
            send_count += 1
        self.log.debug('slc_send: %d', send_count)

    def _slc_add(self, func, slc_def=None):
        """ buffer slc triplet response as (function, flag, value),
            for the given SLC_func byte and slc_def instance providing
            byte attributes ``flag`` and ``val``. If no slc_def is provided,
            the slc definition of ``_slctab`` is used by key ``func``.
        """
        assert len(self._slc_buffer) < self.SLC_MAXSIZE, ('SLC: buffer full')
        if slc_def is None:
            slc_def = self._slctab[func]
        self.log.debug('_slc_add (%s, %s)',
            name_slc_command(func), slc_def)
        self._slc_buffer.extend([func, slc_def.mask, slc_def.val])

    def _slc_process(self, func, slc_def):
        """ Process an SLC definition provided by remote end.

            Ensure the function definition is in-bounds and an SLC option
            we support. Store SLC_VARIABLE changes to self._slctab, keyed
            by SLC byte function ``func``.

            The special definition (0, SLC_DEFAULT|SLC_VARIABLE, 0) has the
            side-effect of replying with a full slc tabset, resetting to
            the default tabset, if indicated.  """
        self.log.debug('_slc_process %s mine=%s, his=%s',
                name_slc_command(func), self._slctab[func], slc_def)

        # out of bounds checking
        if ord(func) > NSLC:
            self.log.warn('SLC not supported (out of range): (%r)', func)
            self._slc_add(func, SLC_nosupport())
            return

        # process special request
        if func == theNULL:
            if slc_def.level == SLC_DEFAULT:
                # client requests we send our default tab,
                self.log.info('SLC_DEFAULT')
                self._default_slc(self._default_tabset)
                self._slc_send()
            elif slc_def.level == SLC_VARIABLE:
                # client requests we send our current tab,
                self.log.info('SLC_VARIABLE')
                self._slc_send()
            else:
                self.log.warn('func(0) flag expected, got %s.', slc_def)
            return

        # evaluate slc
        mylevel, myvalue = (self._slctab[func].level, self._slctab[func].val)
        if slc_def.level == mylevel and myvalue == slc_def.val:
            return
        elif slc_def.level == mylevel and slc_def.ack:
            return
        elif slc_def.ack:
            self.log.debug('slc value mismatch with ack bit set: (%r,%r)',
                    myvalue, slc_def.val)
            return
        else:
            self._slc_change(func, slc_def)

    def _slc_change(self, func, slc_def):
        """ Update SLC tabset with SLC definition provided by remote end.

            Modify prviate attribute ``_slctab`` appropriately for the level
            and value indicated, except for slc tab functions of SLC_NOSUPPORT.

            Reply as appropriate ..
        """
        hislevel, hisvalue = slc_def.level, slc_def.val
        mylevel, myvalue = self._slctab[func].level, self._slctab[func].val
        if hislevel == SLC_NOSUPPORT:
            # client end reports SLC_NOSUPPORT; use a
            # nosupport definition with ack bit set
            self._slctab[func] = SLC_nosupport()
            self._slctab[func].set_flag(SLC_ACK)
            self._slc_add(func)
            return

        if hislevel == SLC_DEFAULT:
            # client end requests we use our default level
            if mylevel == SLC_DEFAULT:
                # client end telling us to use SLC_DEFAULT on an SLC we do not
                # support (such as SYNCH). Set flag to SLC_NOSUPPORT instead
                # of the SLC_DEFAULT value that it begins with
                self._slctab[func].set_mask(SLC_NOSUPPORT)
            else:
                # set current flag to the flag indicated in default tab
                self._slctab[func].set_mask(DEFAULT_SLC_TAB.get(func).mask)
            # set current value to value indicated in default tab
            self._slctab[func].set_value(DEFAULT_SLC_TAB.get(func,
                SLC_nosupport()).val)
            self._slc_add(func)
            return

        # client wants to change to a new value, or,
        # refuses to change to our value, accept their value.
        if self._slctab[func].val != theNULL:
            self._slctab[func].set_value(slc_def.val)
            self._slctab[func].set_mask(slc_def.mask)
            slc_def.set_flag(SLC_ACK)
            self._slc_add(func, slc_def)
            return

        # if our byte value is b'\x00', it is not possible for us to support
        # this request. If our level is default, just ack whatever was sent.
        # it is a value we cannot change.
        if mylevel == SLC_DEFAULT:
            # If our level is default, store & ack whatever was sent
            self._slctab[func].set_mask(slc_def.mask)
            self._slctab[func].set_value(slc_def.val)
            slc_def.set_flag(SLC_ACK)
            self._slc_add(func, slc_def)
        elif slc_def.level == SLC_CANTCHANGE and mylevel == SLC_CANTCHANGE:
            # "degenerate to SLC_NOSUPPORT"
            self._slctab[func].set_mask(SLC_NOSUPPORT)
            self._slc_add(func)
        else:
            # mask current level to levelbits (clears ack),
            self._slctab[func].set_mask(self._slctab[func].level)
            if mylevel == SLC_CANTCHANGE:
                self._slctab[func].val = DEFAULT_SLC_TAB.get(
                        func, SLC_nosupport()).val
            self._slc_add(func)

    def _generate_forwardmask(self):
        """ Generate a 32-byte or 16-byte Forwardmask() instance

            Forwardmask is formed by a bitmask of all 256 possible 8-bit
            keyboard ascii input, or, when not 'outbinary', a 16-byte
            7-bit representation of each value, and whether or not they
            should be "forwarded" by the client on the transport stream
        """
        #   (as opposed to caught locally, such as ^C).
        #
        #   Characters requested to be forwarded are any bytes matching a
        #   supported SLC function byte in self._slctab.
        #
        #   The return value is an instance of ``Forwardmask``, which can
        #   be tested by using the __contains__ method::
        #
        #       if b'\x03' in stream.linemode_forwardmask:
        #           stream.write(b'Press ^C to exit.\r\n')
        if not self.local_option.enabled(BINARY):
            num_bytes, msb = 16, 127
        else:
            num_bytes, msb = 32, 256
        mask32 = [theNULL] * num_bytes
        for mask in range(msb // 8):
            start = mask * 8
            last = start + 7
            byte = theNULL
            for char in range(start, last + 1):
                (func, slc_name, slc_def) = self._slc_snoop(bytes([char]))
                if func is not None and not slc_def.nosupport:
                    # set bit for this character, it is a supported slc char
                    byte = bytes([ord(byte) | 1])
                if char != last:
                    # shift byte left for next character,
                    # except for the final byte.
                    byte = bytes([ord(byte) << 1])
            mask32[mask] = byte
        return Forwardmask(b''.join(mask32), ack=self._forwardmask_enabled)

# Class constructor / set-default routines
#
    def _init_options(self):
        """ Initilize dictionaries ``pending_option``, ``local_option``,
            ``remote_option``, and call ``set_default_linemode()``.
        """
        #: a dictionary of telnet option ``opt`` bytes that follow an
        # *IAC DO* or *DONT* command, and contains a value of ``True``
        # until an *IAC WILL* or *WONT* has been received by remote end.
        # Requests related to extended RFC sub-negotation are keyed by
        # *SB* ``opt``.
        self.pending_option = Option('pending_option', self.log)

        #: a dictionary of telnet option ``byte`` bytes that follow an
        # *IAC WILL* or *WONT* command sent by local end to indicate local
        # capability. For example, if ``local_option[ECHO]`` is ``True``,
        # then this end should echo input received from remote end (note
        # this is clearly not a valid mode for client mode)
        self.local_option = Option('local_option', self.log)

        #: a dictionary of telnet option ``byte`` bytes that follow an
        # *IAC WILL* or *WONT* command received by remote end to indicate
        # remote capability. For example, if remote_option[NAWS] (Negotiate
        # about window size) is True, then the window dimensions of the
        # remote client may be determined by ``request_naws()``
        self.remote_option = Option('remote_option', self.log)

        self.set_default_linemode()

    def _default_callbacks(self):
        """ Set default callback dictionaries ``_iac_callback``,
            ``_slc_callback``, and ``_ext_callback`` to default methods of
            matching names, such that IAC + IP, or, the SLC value negotiated
            for SLC_IP, signals a callback to method ``self.handle_ip``.
        """
        self._iac_callback = {}
        for iac_cmd, key in DEFAULT_IAC_CALLBACKS:
            self.set_iac_callback(iac_cmd, getattr(self, 'handle_%s' % (key,)))

        self._slc_callback = {}
        for slc_cmd, key in DEFAULT_SLC_CALLBACKS:
            self.set_slc_callback(slc_cmd, getattr(self, 'handle_%s' % (key,)))

        # extended callbacks may receive various arguments
        self._ext_callback = {}
        for ext_cmd, key in DEFAULT_EXT_CALLBACKS:
            self.set_ext_callback(ext_cmd, getattr(self, 'handle_%s' % (key,)))

class Linemode(object):
    def __init__(self, mask=LMODE_MODE_LOCAL):
        """ A mask of ``LMODE_MODE_LOCAL`` means that all line editing is
            performed on the client side (default). A mask of theNULL (\x00)
            indicates that editing is performed on the remote side. Valid
            flags are ``LMODE_MODE_TRAPSIG``, ``LMODE_MODE_ACK``,
            ``LMODE_MODE_SOFT_TAB``, ``LMODE_MODE_LIT_ECHO``.
        """
        assert type(mask) is bytes and len(mask) == 1
        self.mask = mask

    def set_flag(self, flag):
        """ Set linemode bitmask ``flag``.  """
        self.mask = bytes([ord(self.mask) | ord(flag)])

    def unset_flag(self, flag):
        """ Unset linemode bitmask ``flag``.  """
        self.mask = bytes([ord(self.mask) ^ ord(flag)])

    @property
    def remote(self):
        """ True if linemode processing is done on server end
            (remote processing).

            """
        return not self.local

    @property
    def local(self):
        """ True if telnet stream is in EDIT mode (local processing).

            When set, the client side of the connection should process all
            input lines, performing any editing functions, and only send
            completed lines to the remote side.

            When unset, client side should *not* process any input from the
            user, and the server side should take care of all character
            processing that needs to be done.
        """
        return bool(ord(self.mask) & ord(LMODE_MODE_LOCAL))

    @property
    def trapsig(self):
        """ True if signals are trapped by client.

        When set, the client side should translate appropriate
        interrupts/signals to their Telnet equivalent.  (These would be
        IP, BRK, AYT, ABORT, EOF, and SUSP)

        When unset, the client should pass interrupts/signals as their
        normal ASCII values, if desired, or, trapped locally.
        """
        return bool(ord(self.mask) & ord(LMODE_MODE_TRAPSIG))

    @property
    def ack(self):
        """ Returns True if ack bit is set.
        """
        return bool(ord(self.mask) & ord(LMODE_MODE_ACK))

    @property
    def soft_tab(self):
        """ When set, the client will expand horizontal tab (\\x09)
            into the appropriate number of spaces.

            When unset, the client should allow horitzontal tab to
            pass through un-modified. This status is only relevant
            for the client end.
        """
        return bool(ord(self.mask) & ord(LMODE_MODE_SOFT_TAB))

    @property
    def lit_echo(self):
        """ When set, non-printable characters are displayed as a literal
            character, allowing control characters to write directly to
            the user's screen.

            When unset, the LIT_ECHO, the client side may echo the character
            in any manner that it desires (fe: '^C' for chr(3)).
        """
        return bool(ord(self.mask) & ord(LMODE_MODE_LIT_ECHO))

    def __str__(self):
        """ Returns string representation of line mode, for debugging """
        if self.mask == bytes([0]):
            return 'remote'
        flags = []
        # we say 'local' to indicate that 'edit' mode means that all
        # input processing is done locally, instead of the obtusely named
        # flag 'edit'
        if self.local:
            flags.append('local')
        else:
            flags.append('remote')
        if self.trapsig:
            flags.append('trapsig')
        if self.soft_tab:
            flags.append('soft_tab')
        if self.lit_echo:
            flags.append('lit_echo')
        if self.ack:
            flags.append('ack')
        return '|'.join(flags)

#: List of globals that may match an iac command option bytes
_DEBUG_OPTS = dict([(value, key)
                    for key, value in globals().items() if key in
                  ('LINEMODE', 'LMODE_FORWARDMASK', 'NAWS', 'NEW_ENVIRON',
                      'ENCRYPT', 'AUTHENTICATION', 'BINARY', 'SGA', 'ECHO',
                      'STATUS', 'TTYPE', 'TSPEED', 'LFLOW', 'XDISPLOC', 'IAC',
                      'DONT', 'DO', 'WONT', 'WILL', 'SE', 'NOP', 'DM', 'TM',
                      'BRK', 'IP', 'ABORT', 'AO', 'AYT', 'EC', 'EL', 'EOR',
                      'GA', 'SB', 'EOF', 'SUSP', 'ABORT', 'LOGOUT',
                      'CHARSET', 'SNDLOC')])

def _name_command(byte):
    """ Given an IAC byte, return its mnumonic global constant. """
    return (repr(byte) if byte not in _DEBUG_OPTS
            else _DEBUG_OPTS[byte])

def _name_commands(cmds, sep=' '):
    return ' '.join([
        _name_command(bytes([byte])) for byte in cmds])

