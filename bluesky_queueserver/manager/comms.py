import threading
import pprint
import json
import asyncio
import uuid
import zmq
import zmq.asyncio
from jsonrpc import JSONRPCResponseManager
from jsonrpc.dispatcher import Dispatcher

import logging

logger = logging.getLogger(__name__)


class CommTimeoutError(TimeoutError):
    """
    Raised when communication error occurs
    """

    pass


class CommJsonRpcError(RuntimeError):
    """
    Raised when returned json-rpc message contains error

    Parameters
    ----------
    message: str
        Error message
    error_code: int
        Error code (returned by `json-rpc`)
    error_type: str
        Error type (returned by `json-rpc` or set to `'CommJsonRpcError'`)
    """

    def __init__(self, message, error_code, error_type):
        super().__init__(message)
        # TODO: change 'code' and 'type' to read-only properties
        self.__error_code = error_code
        self.__error_type = error_type

    @property
    def error_code(self):
        return self.__error_code

    @property
    def error_type(self):
        return self.__error_type

    @property
    def message(self):
        return super().__str__()

    def __str__(self):
        msg = super().__str__() + f"\nError code: {self.error_code}. Error type: {self.error_type}"
        return msg

    def __repr__(self):
        return f"CommJsonRpcError('{self.message}', {self.error_code}, '{self.error_type}')"


def format_jsonrpc_msg(method, params=None, *, notification=False):
    """
    Returns dictionary that contains JSON RPC message.

    Parameters
    ----------
    method: str
        Method name
    params: dict or list, optional
        List of args or dictionary of kwargs.
    notification: boolean
        If the message is notification, no response will be expected.
    """
    msg = {"method": method, "jsonrpc": "2.0"}
    if params is not None:
        msg["params"] = params
    if not notification:
        msg["id"] = str(uuid.uuid4())
    return msg


class PipeJsonRpcReceive:
    """
    The class contains functions for receiving and processing JSON RPC messages received on
    communication pipe.

    Parameters
    ----------
    conn: multiprocessing.Connection
        Reference to bidirectional end of a pipe (multiprocessing.Pipe)
    name: str
        Name of the receiving thread (it is better to assign meaningful unique names to threads.

    Examples
    --------

    .. code-block:: python

        conn1, conn2 = multiprocessing.Pipe()
        pc = PipeJsonRPC(conn=conn1, name="RE QServer Receive")

        def func():
            print("Testing")

        pc.add_handler(func, "some_method")
        pc.start()   # Wait and process commands
        # The function 'func' is called when the message with method=="some_method" is received
        pc.stop()  # Stop before exit to stop the thread.
    """

    def __init__(self, conn, *, name="RE QServer Comm"):
        self._conn = conn
        self._dispatcher = Dispatcher()  # json-rpc dispatcher
        self._thread_running = False  # Set True to exit the thread

        self._thread_name = name

        self._conn_polling_timeout = 0.1  # in sec.

    def start(self):
        """
        Start processing of the pipe messages
        """
        self._start_conn_thread()

    def stop(self):
        """
        Stop processing of the pipe messages (and exit the tread)
        """
        self._thread_running = False

    def __del__(self):
        self.stop()

    def add_method(self, handler, name=None):
        """
        Add method to json-rpc dispatcher.

        Parameters
        ----------
        handler: callable
            Reference to a handler
        name: str, optional
            Name to register (default is the handler name)
        """
        # Add method to json-rpc dispatcher
        self._dispatcher.add_method(handler, name)

    def _start_conn_thread(self):
        if not self._thread_running:
            self._thread_running = True
            self._thread_conn = threading.Thread(
                target=self._receive_conn_thread, name=self._thread_name, daemon=True
            )
            self._thread_conn.start()

    def _receive_conn_thread(self):
        while True:
            if self._conn.poll(self._conn_polling_timeout):
                try:
                    msg = self._conn.recv()
                    # Messages should be handled in the event loop
                    self._conn_received(msg)
                except Exception as ex:
                    logger.exception(
                        "Exception occurred while waiting for RE Manager-> Watchdog message: %s", str(ex)
                    )
                    break
            if not self._thread_running:  # Exit thread
                break

    def _conn_received(self, msg):

        # if logger.level < 11:  # Print output only if logging level is DEBUG (10) or less
        #     msg_json = json.loads(msg)
        #     We don't want to print 'heartbeat' messages
        #     if not isinstance(msg_json, dict) or (msg_json["method"] != "heartbeat"):
        #         logger.debug("Command received RE Manager->Watchdog: %s", pprint.pformat(msg_json))

        response = JSONRPCResponseManager.handle(msg, self._dispatcher)
        if response:
            response = response.json
            self._conn.send(response)


class PipeJsonRpcSendAsync:
    """
    The class contains functions for supporting asyncio-based client for JSON RPC comminucation
    using interprocess communication pipe. The class object must be created on the loop (from one of
    `async` functions). This implementation allows calls only to one method at a time. Multiple
    `send_msg` requests may be put on the loop, but the next message is never sent before
    the response to the previous message is received or timeout occurred.

    Parameters
    ----------
    conn: multiprocessing.Connection
        Reference to bidirectional end of a pipe (multiprocessing.Pipe)
    timeout: float
        Default value of timeout: maximum time to wait for response after a message is sent
    name: str
        Name of the receiving thread (it is better to assign meaningful unique names to threads.

    Examples
    --------

    .. code-block:: python

        conn1, conn2 = multiprocessing.Pipe()

        async def send_messages():
            # Must be instantiated and used within the loop
            p_send = PipeJsonRpcSendAsync(conn=conn1, name="comm-client")
            p_send.start()

            method = "method_name"
            params = {"value": 10}   #   or list of args [10, 25]
            response = await p_send.send_msg(method, params, notification=notification)

            p_send.stop()

        asyncio.run(send_messages())
        pc.stop()


        pc = PipeJsonRpcSendAsync(conn=conn1, name="RE QServer Receive")

        def func():
            print("Testing")

        pc.add_handler(func, "some_method")
        pc.start()   # Wait and process commands
        # The function 'func' is called when the message with method=="some_method" is received
        pc.stop()  # Stop before exit to stop the thread.

    """

    def __init__(self, conn, *, timeout=0.5, name="RE QServer Comm"):
        self._conn = conn
        self._loop = asyncio.get_running_loop()

        self._thread_name = name

        self._fut_comm = None  # Future for waiting for messages from watchdog
        # Lock that prevents sending of the next message before response
        #   to the previous message is received.
        self._lock_comm = asyncio.Lock()
        self._timeout_comm = timeout  # Timeout (time to wait for response to a message)

        # Polling timeout for the pipe. The data will be read from the pipe instantly once it is available.
        #   The timeout determines how long it would take to stop the thread when needed.
        self._conn_polling_timeout = 0.1

        self._thread_running = False  # True - thread is running

        # Expected ID of the received message. The ID must be the same as the ID of the sent message.
        #   Ignore all message that don't have matching ID or no ID.
        self._expected_msg_id = None

    def start(self):
        """
        Start processing of the pipe messages
        """
        self._start_conn_thread()

    def stop(self):
        """
        Stop processing of the pipe messages (and exit the tread)
        """
        self._thread_running = False

    def __del__(self):
        self.stop()

    def _start_conn_thread(self):
        # Start 'receive' thread
        if not self._thread_running:
            self._thread_running = True
            self._pipe_receive_thread = threading.Thread(
                target=self._pipe_receive, name=self._thread_name, daemon=True
            )
            self._pipe_receive_thread.start()

    async def send_msg(self, method, params=None, *, notification=False, timeout=None):
        """
        Send JSON RPC message to server and return the result of the function (method)
        or raise exception in case of an error. Returns None if the message is notification.

        Parameters
        ----------
        method: str
            name of JSON RPC method
        params: list or dict
            args or kwargs of the remote method
        notification: boolean
            True - message is notification. The function returns immediately without
            waiting of the response, which is never generated for notification.
        timeout: float
            Timeout in seconds. If no response is received at expiration of timeout,
            `CommTimeoutError` is raised. If the response will be received later, it
            will be ignored.

        Raises
        ------
        CommTimeoutError
            Timeout occurred. Response is not received in time
        CommJsonRpcError
            Error occurred while processing the message. This could indicate an error
            in `json-rpc` package (e.g. method not found) or exception raised by
            the method itself. It is recommended that the methods catch and process
            their exceptions (may be except parameter validation) and leave
            `CommJsonRpcError` for reporting `json-rpc` errors. In well tested
            program this exception should never be raised.
        RuntimeError
            Unrecognized message received (message doesn't contain `result` or `error`
            keys. This should never happen in well tested program.

        The function will raise `CommTimeoutError` in case of communication timeout
        """
        # The lock protects from sending the next message
        #   before response to the previous message is received.

        if timeout is None:
            timeout = self._timeout_comm

        async with self._lock_comm:
            msg = format_jsonrpc_msg(method, params, notification=notification)
            try:
                msg_json = json.dumps(msg)
                self._conn.send(msg_json)

                # No response is expected if this is a notification
                if not notification:
                    self._expected_msg_id = msg["id"]
                    self._fut_comm = self._loop.create_future()
                    # Waiting for the future may raise 'asyncio.TimeoutError'
                    await asyncio.wait_for(self._fut_comm, timeout=timeout)
                    response = self._fut_comm.result()

                    if "result" in response:
                        return response["result"]
                    elif "error" in response:
                        # TODO: verify that this is all information that should be saved
                        err_code = response["error"]["code"]
                        if "data" in response["error"]:
                            # Server Error (issue with execution of the method)
                            err_type = response["error"]["data"]["type"]
                            # Message: "Server error: <message text>"
                            err_msg = f'{response["error"]["message"]}: {response["error"]["data"]["message"]}'
                        else:
                            # Other json-rpc errors
                            err_type = "CommJsonRpcError"
                            err_msg = response["error"]["message"]
                        raise CommJsonRpcError(err_msg, error_code=err_code, error_type=err_type)
                    else:
                        err_msg = (
                            f"Message {pprint.pformat(msg)}\n"
                            f"resulted in response with unknown format: {pprint.pformat(response)}"
                        )
                        raise RuntimeError(err_msg)
                else:
                    response = None
                return response

            except asyncio.TimeoutError:
                raise CommTimeoutError(f"Timeout while waiting for response to message: \n{pprint.pformat(msg)}")
            finally:
                self._fut_comm = None
                self._expected_msg_id = None

    async def _response_received(self, response):
        """
        Set the future with the results. Ignore all messages with unexpected or missing IDs.
        Also ignore all unexpected messages.
        """
        if self._expected_msg_id is not None:
            if "id" in response:
                if response["id"] != self._expected_msg_id:
                    # Incorrect ID: ignore the message.
                    logger.error(
                        "Received response with incorrect message ID: %s. Expected %s.\nMessage: %s",
                        response["id"],
                        self._expected_msg_id,
                        pprint.pformat(response),
                    )
                else:
                    # Accept the message. Otherwise wait for timeout
                    self._fut_comm.set_result(response)
            else:
                # Missing ID: ignore the message
                logger.error("Received response with missing message ID: %s", pprint.pformat(response))
        else:
            logger.error(
                "Unsolicited message received: %s. Message is ignored",
                pprint.pformat(response),
            )

    def _conn_received(self, response):
        asyncio.create_task(self._response_received(response))

    def _pipe_receive(self):
        while True:
            if self._conn.poll(self._conn_polling_timeout):
                try:
                    msg_json = self._conn.recv()
                    msg = json.loads(msg_json)
                    # logger.debug("Message Watchdog->Manager received: '%s'", pprint.pformat(msg))
                    # Messages should be handled in the event loop
                    self._loop.call_soon_threadsafe(self._conn_received, msg)
                except Exception as ex:
                    logger.exception("Exception occurred while waiting for packet: %s", str(ex))
                    break
            if not self._thread_running:  # Exit thread
                break


class ZMQCommSendAsync:
    """
    API for communication with RE Manager via ZMQ. The object has to be created
    from the running even loop or the loop has to be passed as a parameter during
    initialization.

    Parameters
    ----------
    loop: asyncio loop
        Current event loop
    zmq_server_address: str or None
        Address of ZMQ server. If None, then the default address is ``tcp://localhost:5555``
        is used.
    timeout_recv: int
        Timeout (in ms) for ZMQ receive operations.
    timeout_send: int
        Timeout (in ms) for ZMQ send operations.
    raise_timeout_exceptions: bool
        Tells if exceptions should be raised in case of communication errors (mostly timeouts)
        when ``send_message()`` is executed. Exception``CommTimeoutError`` is raised if the
        parameter is ``True``, otherwise error message is returned by ``send_message()``.

    Examples
    --------

    .. code-block: python

        async def communicate():
            zmq_comm = ZMQCommSendAsync()
            for n in range(10):
                msg = await send_message(method="some_method", params={"some_value": n}
                print(f"msg={msg}")

        asyncio.run(communicate())
    """

    def __init__(
        self,
        *,
        loop=None,
        zmq_server_address=None,
        timeout_recv=2000,
        timeout_send=500,
        raise_timeout_exceptions=False,
    ):
        self._loop = loop if loop else asyncio.get_event_loop()

        zmq_server_address = zmq_server_address or "tcp://localhost:5555"

        self._timeout_receive = timeout_recv  # Timeout for 'recv' operation (ms)
        self._timeout_send = timeout_send  # # Timeout for 'send' operation (ms)
        self._raise_timeout_exceptions = raise_timeout_exceptions

        # ZeroMQ communication
        self._ctx = zmq.asyncio.Context()
        self._zmq_socket = None
        self._zmq_server_address = zmq_server_address

        self._zmq_socket_open()
        self._lock_zmq = asyncio.Lock()

    def __del__(self):
        self._zmq_socket.close()

    def get_loop(self):
        """
        Returns the asyncio event loop.
        """
        return self._loop

    async def _zmq_send(self, msg):
        await self._zmq_socket.send_json(msg)

    async def _zmq_receive(self):
        try:
            msg = await self._zmq_socket.recv_json()
        except Exception as ex:
            # Timeout occurred. Socket needs to be reset.
            logger.exception("ZeroMQ communication failed: %s" % str(ex))
            raise
        return msg

    async def _zmq_communicate(self, msg_out):
        await self._zmq_send(msg_out)
        msg_in = await self._zmq_receive()
        return msg_in

    def _zmq_socket_open(self):
        self._zmq_socket = self._ctx.socket(zmq.REQ)
        self._zmq_socket.RCVTIMEO = self._timeout_receive
        self._zmq_socket.SNDTIMEO = self._timeout_send
        # Clear the buffer quickly after the socket is closed
        self._zmq_socket.setsockopt(zmq.LINGER, 100)

        if self._zmq_socket.connect(self._zmq_server_address):
            msg_err = f"Failed to connect to the server '{self._zmq_server_address}'"
            raise RuntimeError(msg_err)

        logger.info("Connected to ZeroMQ server '%s'" % str(self._zmq_server_address))

    def _zmq_socket_restart(self):
        self._zmq_socket.close()
        self._zmq_socket_open()

    def _create_msg(self, *, method, params=None):
        return {"method": method, "params": params}

    async def send_message(self, *, method, params=None, raise_exceptions=False):
        """
        Send message to ZMQ server and wait for the response. The message must contain
        a name of a method supported by the server and a dictionary of parameters that
        are required by the method. In case of communication error (timeout), the function
        returns error message or raises ``CommTimeoutError`` exception depending on
        the setting of ``raise_timeout_exceptions`` property.

        Parameters
        ----------
        method: str
            Name of the method to be invoked on the server. The method must be supported
            by the server.
        params: dict or None
            Dictionary of parameters passed to the method. If ``None`` then empty dictionar
            is passed to the server.

        Returns
        -------
        dict
            Message returned by the server.

        Raises
        ------
        CommTimeoutError
            Raised if communication error occurs and ``raise_timeout_exceptions`` is set ``True``.
        """

        # Send empty dictionary if no parameters are passed
        params = params or {}

        async with self._lock_zmq:
            try:
                msg_out = self._create_msg(method=method, params=params)
                msg_in = await self._zmq_communicate(msg_out)
            except Exception as ex:
                # This is very likely a timeout (RE Manager is not responding)
                self._zmq_socket_restart()
                errmsg = f"ZMQ communication error: {str(ex)}"
                if self._raise_timeout_exceptions:
                    raise CommTimeoutError(errmsg)
                msg_in = {"success": False, "msg": errmsg}
            return msg_in


def zmq_single_request(method, params=None, *, zmq_server_address=None):
    """
    Send a single request to ZMQ server. The function opens the socket, sends
    a single ZMQ request and closes the socket. The function is not expected
    to raise exceptions. In case of communication error the return value
    of ``msg`` is ``None`` and ``err_msg`` contains the error message. Otherwise
    ``err_msg`` is empty and ``msg`` contains the dictionary returned by the server.

    Parameters
    ----------
    method: str
        Name of the method called in RE Manager
    params: dict or None
        Dictionary of parameters (payload of the message). If ``None`` then
        the message is sent with empty payload: ``params = {}``.

    Returns
    -------
    msg: dict or None
        Message received from RE Manager in response to the request. None if communication
        error (timeout) occurred.
    err_msg: str
        Contains a message in case communication error (timeout) occurs. Empty string otherwise.
    """

    msg_received = None

    async def send_request(method, params):
        nonlocal msg_received
        zmq_to_manager = ZMQCommSendAsync(zmq_server_address=zmq_server_address)
        msg_received = await zmq_to_manager.send_message(method=method, params=params)
        del zmq_to_manager  # This will close the socket

    try:
        asyncio.run(send_request(method, params))

        msg = msg_received
        msg_err = ""
    except Exception as ex:
        msg = None
        msg_err = str(ex)

    if msg_err:
        logger.warning("Communication with RE Manager failed: %s", str(msg_err))

    return msg, msg_err
