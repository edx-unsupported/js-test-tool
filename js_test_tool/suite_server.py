"""
Serve test runner pages and included JavaScript files on a local port.
"""

from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
import threading
import re
import pkg_resources
import os.path
import logging
import json
import time
import mimetypes
from abc import ABCMeta, abstractmethod
from js_test_tool.coverage import SrcInstrumenter, SrcInstrumenterError, CoverageData


LOGGER = logging.getLogger(__name__)


class TimeoutError(Exception):
    """
    The server timed out while waiting.
    """
    pass


class DuplicateSuiteNameError(Exception):
    """
    Two or more suites have the same name.
    """
    pass


class SuitePageServer(HTTPServer):
    """
    Serve test suite pages and included JavaScript files.
    """

    protocol_version = 'HTTP/1.1'

    # Amount of time to wait for clients to POST coverage info
    # back to the server before timing out.
    COVERAGE_TIMEOUT = 2.0

    # Amount of time to wait between checks that the we
    # have all the coverage info
    COVERAGE_WAIT_TIME = 0.1

    # Returns the `CoverageData` instance used by the server
    # to store coverage data received from the test suites.
    # Since `CoverageData` is thread-safe, it is okay for
    # other processes to write to it asynchronously.
    coverage_data = None

    def __init__(self, suite_desc_list, suite_renderer, jscover_path=None):
        """
        Initialize the server to serve test runner pages
        and dependencies described by `suite_desc_list`
        (list of `SuiteDescription` instances).

        `jscover_path` is the path to the JSCover JAR file.  If not
        specified, no coverage information will be collected.

        Use `suite_renderer` (a `SuiteRenderer` instance) to
        render the test suite pages.
        """

        # Store dependencies
        self.desc_dict = self._suite_dict_from_list(suite_desc_list)
        self.renderer = suite_renderer
        self._jscover_path = jscover_path

        # Create a dict for source instrumenter services
        # (One for each suite description)
        self.src_instr_dict = {}

        # Using port 0 assigns us an unused port
        address = ('127.0.0.1', 0)
        HTTPServer.__init__(self, address, SuitePageRequestHandler)

    def start(self):
        """
        Start serving pages on an open local port.
        """
        server_thread = threading.Thread(target=self.serve_forever)
        server_thread.daemon = True
        server_thread.start()

        # If we're collecting coverage information
        if self._jscover_path is not None:

            # Create an object to store coverage data we receive
            self.coverage_data = CoverageData()

            # Start each SrcInstrumenter instance if we know where JSCover is
            for suite_name, desc in self.desc_dict.iteritems():

                # Inform the coverage data that we expect this source
                # (report it as 0% if no info received).
                for rel_path in desc.src_paths():
                    self.coverage_data.add_expected_src(desc.root_dir(), rel_path)

                # Create an instrumenter serving files
                # in the suite description root directory
                instr = SrcInstrumenter(desc.root_dir(),
                                        tool_path=self._jscover_path)

                # Start the instrumenter service
                instr.start()

                # Associate the instrumenter with its suite description
                self.src_instr_dict[suite_name] = instr

        else:
            self.src_instr_dict = {}

    def stop(self):
        """
        Stop the server and free the port.
        """

        # Stop each instrumenter service that we started
        for instr in self.src_instr_dict.values():
            instr.stop()

        # Stop the page server and free the port
        self.shutdown()
        self.socket.close()

    def suite_url_list(self):
        """
        Return a list of URLs (unicode strings), where each URL
        is a test suite page containing the JS code to run
        the JavaScript tests.
        """
        return [self.root_url() + u'suite/{}'.format(suite_name)
                for suite_name in self.desc_dict.keys()]

    def root_url(self):
        """
        Return the root URL (including host and port) for the server
        as a unicode string.
        """
        host, port = self.server_address
        return u"http://{}:{}/".format(host, port)

    def all_coverage_data(self):
        """
        Returns a `CoverageData` instance containing all coverage data
        received from running the tests.

        Blocks until all suites have reported coverage data.  If it
        times out waiting for all data, raises a `TimeoutException`.

        If we are not collecting coverage, returns None.
        """
        if self.coverage_data is not None:
            self._block_until(self._has_all_coverage)
            return self.coverage_data

        else:
            return None

    def _block_until(self, success_func):
        """
        Block until `success_func` returns True.
        `success_func` should be a lambda with no argument.
        """

        # Remember when we started
        start_time = time.time()

        # Until we are successful
        while not success_func():

            # See if we've timed out
            if time.time() - start_time > self.COVERAGE_TIMEOUT:
                raise TimeoutError()

            # Wait a little bit before checking again
            time.sleep(self.COVERAGE_WAIT_TIME)

    def _has_all_coverage(self):
        """
        Returns True if and only if every suite
        has coverage information.
        """
        # Retrieve the indices of each suite for which coverage
        # information was reported.
        suite_name_list = self.coverage_data.suite_name_list()

        # Check that we have an index for every suite
        # (This is not the most efficient way to do this --
        # if it becomes a bottleneck, we can revisit.)
        return (sorted(suite_name_list) == sorted(self.desc_dict.keys()))

    @staticmethod
    def _suite_dict_from_list(suite_desc_list):
        """
        Given a list of `SuiteDescription` instances, construct
        a dictionary mapping suite names to the instances.

        Raises a `DuplicateSuiteNameError` if two suites have
        the same name.
        """
        suite_dict = {
            suite.suite_name(): suite
            for suite in suite_desc_list
        }

        # Check that we haven't repeated keys
        if len(suite_dict) < len(suite_desc_list):
            raise DuplicateSuiteNameError("Two or more test suites have the same name")

        return suite_dict


class BasePageHandler(object):
    """
    Abstract base class for page handler.  Checks whether
    it can handle a given URL path.  If it can, it then generates
    the page contents.
    """

    __metaclass__ = ABCMeta

    # HTTP methods handled by this class
    # The default is to handle only GET methods
    HTTP_METHODS = ["GET"]

    # Subclasses override this to provide a regex that matches
    # URL paths.  Should be a `re` module compiled regex.
    PATH_REGEX = None

    def page_contents(self, path, method, content):
        """
        Returns a `(content, mime_type)` tuple if the page
        could be loaded.  Otherwise, returns `(None, None)`.
        `content` is a unicode string representing the page contents;
        `mime_type` is the MIME type to send as the Content-Header
        in the response.

        `method` is the HTTP method used to load the page (e.g. "GET" or "POST")
        `content` is the content of the HTTP request.
        """

        # Check that we handle this kind of request
        if method in self.HTTP_METHODS:

            # Check whether this handler matches the URL path
            result = self.PATH_REGEX.match(path)

            # If this is not a match, return None
            if result is None:
                return (None, None)

            # If we do match, attempt to load the page.
            else:
                page_contents = self.load_page(method, content, *result.groups())
                mime_type = self.mime_type(method, content, *result.groups())
                return (page_contents, mime_type)

        else:
            return (None, None)

    @abstractmethod
    def load_page(self, method, content, *args):
        """
        Subclasses override this to load the page.
        `args` is a list of arguments parsed using the regular expression.

        If the page cannot be loaded (e.g. accessing a file that
        does not exist), then return None.

        `method` is the HTTP method used to load the page (e.g. "GET" or "POST")
        `content` is the content of the HTTP request.
        """
        pass

    @abstractmethod
    def mime_type(self, method, content, *args):
        """
        Subclasses override this to return the MIME type
        for the page.

        Arguments have the same meaning as in `load_page()`.
        """
        pass

    @staticmethod
    def guess_mime_type(url):
        """
        Guess the mime type for a given URL by its
        extension; default to text/plain.
        """
        mime_type, _ = mimetypes.guess_type(url)
        if mime_type is None:
            mime_type = 'text/plain'
        return mime_type


class SuitePageHandler(BasePageHandler):
    """
    Handle requests for paths of the form `/suite/SUITE_NAME`, where
    `SUITE_NAME` is the name of the test suite description.
    Serves the suite runner page.
    """

    # Handle requests to /suite/NAME/
    # Ignore GET parameters
    PATH_REGEX = re.compile(r'^/suite/([^?/]+)/?(\?.*)?$')

    def __init__(self, renderer, desc_dict):
        """
        Initialize the `SuitePageHandler` to use `renderer`
        (a `SuiteRenderer` instance) and `desc_dict` (a dict
        mapping suite names to `SuiteDescription` instances).
        """
        super(SuitePageHandler, self).__init__()
        self._renderer = renderer
        self._desc_dict = desc_dict

    def load_page(self, method, content, *args):
        """
        Render the suite runner page.
        """

        # The only arg should be the suite name
        suite_name = args[0]

        # Try to find the suite description
        suite_desc = self._desc_dict.get(suite_name)

        # If we can't find it, don't serve it
        if suite_desc is None:
            return None

        # Otherwise, render the page
        else:
            return self._renderer.render_to_string(suite_name, suite_desc)

    def mime_type(self, method, content, *args):
        """
        Return the MIME type for the page.
        """
        return 'text/html'


class RunnerPageHandler(BasePageHandler):
    """
    Handle requests for paths of the form '/runner/RUNNER_PATH', where
    `RUNNER_PATH` is a page that runs JavaScript tests.
    """

    # Handle requests to /runner/ pages, ignoring
    # GET parameters
    PATH_REGEX = re.compile(r'^/runner/([^\?]+).*$')

    def load_page(self, method, content, *args):
        """
        Load the runner file from this package's resources.
        """

        # Only arg should be the relative path
        rel_path = os.path.join('runner', args[0])

        # Attempt to load the package resource
        try:
            content = pkg_resources.resource_string('js_test_tool', rel_path)

        # If we could not load it, return None
        except BaseException:
            return None

        # If we successfully loaded it, return a unicode str
        else:
            return content.decode()

    def mime_type(self, method, content, *args):
        """
        Return the MIME type for the page.
        """
        return self.guess_mime_type(args[0])


class DependencyPageHandler(BasePageHandler):
    """
    Load dependencies required by the test suite description.
    """

    # Parse the suite name and relative path,
    # ignoring any GET parameters in the URL.
    PATH_REGEX = re.compile('^/suite/([^/]+)/include/([^?]+).*$')

    def __init__(self, desc_dict):
        """
        Initialize the dependency page handler to serve dependencies
        specified by `desc_dict` (a dict mapping suite names to 
        `SuiteDescription` instances).
        """
        super(DependencyPageHandler, self).__init__()
        self._desc_dict = desc_dict

    def load_page(self, method, content, *args):
        """
        Load the test suite dependency file, using a path relative
        to the description file.

        Returns a `unicode` string of the page contents.
        """

        # Interpret the arguments (from the regex)
        suite_name, rel_path = args

        # Retrieve the full path to the dependency, if it exists
        # and is specified in the test suite description
        full_path = self._dependency_path(suite_name, rel_path)

        if full_path is not None:

            # Load the file
            try:
                with open(full_path) as file_handle:
                    contents = file_handle.read()

            # If we cannot load the file (probably because it doesn't exist)
            # then return None
            except IOError:
                return None

            # Successfully loaded the file; return the contents as a unicode str
            else:

                # First try to decode as UTF-8
                try:
                    return contents.decode('utf-8')

                # If we can't decode as UTF-8, try ISO-8859-1
                except UnicodeDecodeError:
                    return contents.decode('iso-8859-1')

        # If this is not one of our listed dependencies, return None
        else:
            return None

    def mime_type(self, method, content, *args):
        """
        Return the MIME type for the page.
        """
        _, rel_path = args
        return self.guess_mime_type(rel_path)


    def _dependency_path(self, suite_name, path):
        """
        Return the full filesystem path to the dependency, if it
        is specified in the test suite description with name `suite_name`.
        Otherwise, return None.
        """

        # Try to find the suite description with `suite_name`
        suite_desc = self._desc_dict.get(suite_name)

        # If we can't find it, give up
        if suite_desc is None:
            return None

        # Get all dependency paths
        all_paths = (suite_desc.lib_paths() +
                     suite_desc.src_paths() +
                     suite_desc.spec_paths() +
                     suite_desc.fixture_paths())

        # If the path is in our listed dependencies, we can serve it
        if path in all_paths:

            # Resolve the full filesystem path
            return os.path.join(suite_desc.root_dir(), path)

        else:

            # If we did not find the path, we cannot serve it
            return None


class InstrumentedSrcPageHandler(BasePageHandler):
    """
    Instrument the JavaScript source file to collect coverage information.
    """

    PATH_REGEX = re.compile('^/suite/([^/]+)/include/([^?]+).*$')

    def __init__(self, desc_dict, instr_dict):
        """
        Initialize the dependency page handler to serve dependencies
        specified by `desc_dict` (a dict mapping suite names
        to `SuiteDescription` instances).

        `instr_dict` is a dict mapping suite names to 
        `SrcInstrumenter` instances.  There should be one
        instrumenter for each suite.
        """
        super(InstrumentedSrcPageHandler, self).__init__()
        self._desc_dict = desc_dict
        self._instr_dict = instr_dict

    def load_page(self, method, content, *args):
        """
        Load an instrumented version of the JS source file.
        """

        # Interpret the arguments (from the regex)
        suite_name, rel_path = args

        # Check that this is a source file (not a lib or spec)
        if self._is_src_file(suite_name, rel_path):

            # Send the instrumented source (delegating to JSCover)
            return self._send_instrumented_src(suite_name, rel_path)

        # If not a source file, do not handle it.
        # Expect the non-instrumenting page handler to serve
        # the page instead
        else:
            return None

    def mime_type(self, method, content, *args):
        """
        Return the MIME type for the page.
        """
        _, rel_path = args
        return self.guess_mime_type(rel_path)

    def _send_instrumented_src(self, suite_name, rel_path):
        """
        Return an instrumented version of the JS source file at `rel_path`
        for the suite with name `suite_name`, or None if the source
        could not be loaded.
        """

        # Try to retrieve the instrumenter
        instr = self._instr_dict.get(suite_name)

        if instr is None:
            msg = "Could not find instrumenter for '{}'".format(suite_name)
            LOGGER.warning(msg)
            return None

        try:

            # This performs a synchronous call to the instrumenter
            # service, raising an exception if it cannot retrieve
            # the instrumented version of the source.
            return instr.instrumented_src(rel_path)

        # If we cannot get the instrumented source,
        # return None.  This should cause the un-instrumented
        # version of the source to be served (when another
        # handler matches the URL regex)
        except SrcInstrumenterError as err:
            msg = "Could not retrieve instrumented version of '{}': {}".format(rel_path, err)
            LOGGER.warning(msg)
            return None

    def _is_src_file(self, suite_name, rel_path):
        """
        Returns True only if the file at `rel_path` is a source file
        in the suite named `suite_name`.
        """

        suite_desc = self._desc_dict.get(suite_name)

        if suite_desc is None:
            return False

        return (rel_path in suite_desc.src_paths())


class StoreCoveragePageHandler(BasePageHandler):
    """
    Store coverage reports POSTed back to the server
    by clients running instrumented JavaScript sources.
    """

    PATH_REGEX = re.compile('^/jscoverage-store/([^/]+)/?$')

    # Handle only POST
    HTTP_METHODS = ["POST"]

    def __init__(self, desc_dict, coverage_data):
        """
        Initialize the dependency page handler to serve dependencies
        specified by `desc_dict` (a dict mapping suite names to 
        `SuiteDescription` instances).

        `coverage_data` is the `CoverageData` instance to send
        any received coverage data to.
        """
        super(StoreCoveragePageHandler, self).__init__()
        self._desc_dict = desc_dict
        self._coverage_data = coverage_data

    def load_page(self, method, content, *args):
        """
        Send the coverage information to the server.
        """

        # Retrieve the suite name from the URL
        suite_name = args[0]

        # Store the coverage data
        return self._store_coverage_data(suite_name, content)

    def mime_type(self, method, content, *args):
        """
        Return the MIME type for the page.
        """
        return 'text/plain'

    def _store_coverage_data(self, suite_name, request_content):
        """
        Store received coverage data for the JS source file
        in the suite with name `suite_name`.

        `request_content` is the content of the HTTP POST request.

        Returns None if any errors occur; returns a success method if successful.
        """

        # Record that we got a coverage report for this suite
        self._coverage_data.add_suite_name(suite_name)

        # Retrieve the root directory for this suite
        suite_desc = self._desc_dict.get(suite_name)

        # If we can't find the suite description, give up
        if suite_desc is None:
            return None

        try:
            # Parse the request content as JSON
            coverage_dict = json.loads(request_content)

            if not isinstance(coverage_dict, dict):
                raise ValueError()

            # `CoverageData.load_from_dict()` is thread-safe, so it
            # is okay to write to this, even if the request handler
            # is running asynchronously.
            self._coverage_data.load_from_dict(suite_desc.root_dir(),
                                               suite_desc.prepend_path(),
                                               coverage_dict)

        except ValueError:
            msg = ("Could not interpret coverage data in POST request " +
                   "to suite {}: {}".format(suite_name, request_content))
            LOGGER.warning(msg)
            return None

        else:
            return "Success: coverage data received"


class SuitePageRequestHandler(BaseHTTPRequestHandler):
    """
    Handle HTTP requsts to the `SuitePageServer`.
    """

    protocol = "HTTP/1.0"

    def __init__(self, request, client_address, server):

        # Initialize the page handlers
        # We always handle suite runner pages, and
        # the runner dependencies (e.g. jasmine.js)
        self._page_handlers = [SuitePageHandler(server.renderer, server.desc_dict),
                               RunnerPageHandler()]

        # If we are configured for coverage, add another handler
        # to serve instrumented versions of the source files.
        if len(server.src_instr_dict) > 0:

            # Create the handler to serve instrumented JS pages
            instr_src_handler = InstrumentedSrcPageHandler(server.desc_dict,
                                                           server.src_instr_dict)
            self._page_handlers.append(instr_src_handler)

            # Create a handler to store coverage data POSTed back
            # to the server from the client.
            store_coverage_handler = StoreCoveragePageHandler(server.desc_dict,
                                                              server.coverage_data)
            self._page_handlers.append(store_coverage_handler)

        # We always serve dependencies.  If running with coverage,
        # the instrumented src handler will intercept source files.
        # Serving the un-instrumented version is the fallback, and
        # will still be used for library/spec dependencies.
        self._page_handlers.append(DependencyPageHandler(server.desc_dict))

        # Call the superclass implementation
        # This will immediately call do_GET() if the request is a GET
        BaseHTTPRequestHandler.__init__(self, request, client_address, server)

    def do_GET(self):
        """
        Serve suite runner pages and JavaScript dependencies.
        """
        self._handle_request("GET")

    def do_POST(self):
        """
        Respond to POST requests providing coverage information.
        """
        self._handle_request("POST")

    def log_message(self, format_str, *args):
        """
        Override the base-class logger to avoid
        spamming the console.
        """
        LOGGER.debug("{} -- [{}] {}".format(self.client_address[0],
                                            self.log_date_time_string(),
                                            format_str % args))

    def _handle_request(self, method):
        """
        Handle an HTTP request of type `method` (e.g. "GET" or "POST")
        """

        # Get the request content
        request_content = self._content()

        for handler in self._page_handlers:

            # Try to retrieve the page
            content, mime_type = handler.page_contents(self.path, method, request_content)

            # If we got a page, send the contents
            if content is not None:
                self._send_response(200, content, mime_type)
                return

        # If we could not retrieve the contents (e.g. because
        # the file does not exist), send an error response
        self._send_response(404, None, 'text/plain')

    def _send_response(self, status_code, content, mime_type):
        """
        Send a response to an HTTP request as UTF-8 encoded HTML.
        `content` can be empty, None, or a UTF-8 string.
        `mime_type` is sent as the Content-Type header.
        """

        self.send_response(status_code)
        self.send_header('Content-Type', mime_type + '; charset=utf-8')
        self.send_header('Content-Language', 'en')
        self.end_headers()

        if content:
            self.wfile.write(content.encode('utf8'))

    def _content(self):
        """
        Retrieve the content of the request.
        """
        try:
            length = int(self.headers.getheader('content-length'))
        except (TypeError, ValueError):
            return ""
        else:
            return self.rfile.read(length)
