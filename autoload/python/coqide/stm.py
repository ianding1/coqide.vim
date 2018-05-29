'''Coq state machine.

In this module defines STM, a Coq state machine.

Each Coq document being edited contains a state machine that records
the execution state of the document.
'''

import functools
import logging
import queue
import threading

from . import xmlprotocol as xp
from . import actions
from .sentence import Sentence, Mark


logger = logging.getLogger(__name__)        # pylint: disable=C0103


class _SequentialTaskThread:
    '''A thread that executes the tasks in the order that they are emitted.

    Another task must be executed only after one task has already been done.
    For example, when adding a bulk of sentences, the Add request of the second sentence
    requires the state id of the first, which is available only after receiving the response.

    This class manages the execution order of the tasks. Only after a task calls the `done` callback
    will the following get executed.
    '''

    def __init__(self):
        '''Create a thread that executes the tasks in sequence.'''
        self._closing = False
        self._scheduled_task_count = 0
        schedule_queue = queue.Queue(100)
        task_done = threading.Semaphore(0)

        def done():
            '''The callback to resume the task processing loop.'''
            task_done.release()

        def thread_main():
            '''Repeat executing the tasks and waiting for them to be done.'''
            while True:
                task, name = schedule_queue.get()
                if task is None or self._closing:
                    break
                logger.debug('Runs task: %s ...', name)
                task(done)
                task_done.acquire()
                self._scheduled_task_count -= 1
                logger.debug('Finish task: %s.', name)
            logger.debug('_SequentialTaskThread quits normally.')

        thread = threading.Thread(target=thread_main)
        thread.start()
        logger.debug('_SequentialTaskThread starts.')

        self._schedule_queue = schedule_queue
        self._thread = thread

    def schedule(self, task, name):
        '''Schedule a task for running.

        `task` should be a unary function that receives a callback. The task calls
        the callback when done to notify the thread to execute the next.
        '''
        assert task is not None
        self._scheduled_task_count += 1
        self._schedule_queue.put((task, name))

    def shutdown_join(self):
        '''Discard all the pending tasks and wait for the thread to quit.'''
        self._closing = True
        try:
            self._schedule_queue.put_nowait((None, None))
        except queue.Full:
            pass
        self._thread.join()

    def discard_scheduled_tasks(self):
        '''Discard all the scheduled tasks in the queue.

        It is called when the failure of a task cancels the following.
        '''
        try:
            while True:
                self._schedule_queue.get_nowait()
                self._scheduled_task_count -= 1
        except queue.Empty:
            pass

    def scheduled_task_count(self):
        '''Return the number of scheduled tasks (including the running task).'''
        return self._scheduled_task_count


def _finally_call(cleanup):
    '''A function decorator that call `cleanup` on the exit of the decorated function
    no matter what.'''
    def wrapper(func):                  # pylint: disable=C0111
        @functools.wraps(func)
        def wrapped(*args, **kwargs):   # pylint: disable=C0111
            try:
                return func(*args, **kwargs)
            finally:
                cleanup()
        return wrapped
    return wrapper


class _FeedbackHandler:                                # pylint: disable=R0903
    '''A functional that handles the feedbacks.'''

    _FEEDBACK_HANDLERS = [
        [xp.ErrorMsg, '_on_error_msg'],
        [xp.Message, '_on_message'],
        [xp.Processed, '_on_processed'],
        [xp.AddedAxiom, '_on_added_axiom'],
    ]

    def __init__(self, state_id_map, handle_action):
        '''Create a feedback handler.

        The handler requires the property `state_id_map` of the STM object in a read-only manner.
        '''
        self._state_id_map = state_id_map
        self._handle_action = handle_action

    def __call__(self, feedback):
        '''Process the given feedback.

        If the feedback is processed, return True. Otherwise, return False.'''
        sentence = self._state_id_map.get(feedback.state_id, None)

        for fb_type, fb_handler_name in self._FEEDBACK_HANDLERS:
            if isinstance(feedback.content, fb_type):
                logger.debug('FeedbackHandler processes: %s', feedback)
                handler = getattr(self, fb_handler_name)
                handler(feedback.content, sentence)
                return True
        return False

    def _on_error_msg(self, error_msg, sentence):
        '''Process ErrorMsg.'''
        if sentence:
            sentence.set_error(error_msg.location, error_msg.message, self._handle_action)
        else:
            self._handle_action(actions.ShowMessage(error_msg.message, 'error'))

    def _on_message(self, message, sentence):
        '''Process Message.'''
        if message.level == 'error':
            if sentence:
                sentence.set_error(message.location, message.message, self._handle_action)
            else:
                self._handle_action(actions.ShowMessage(message.message, 'error'))
        else:
            self._handle_action(actions.ShowMessage(message.message, message.level))

    def _on_processed(self, _, sentence):
        '''Highlight the sentence to "Processed" state.'''
        if sentence:
            sentence.set_processed(self._handle_action)

    def _on_added_axiom(self, _, sentence):
        '''Highlight the sentence to "Unsafe" state.'''
        if sentence:
            sentence.set_axiom(self._handle_action)


class _Document:
    '''This class manages the sentences in a document.'''

    def __init__(self):
        '''Create a new Document.'''
        self.sentences = []
        self.state_id_map = {}
        self.line_map = {}

    def add(self, sentence, tip):
        '''Add a sentence.'''
        if tip:
            index = self.sentences.index(tip) + 1
        else:
            index = len(self.sentences)

        self.sentences.insert(index, sentence)
        self.state_id_map[sentence.state_id] = sentence

        start_line = sentence.region.start.line
        stop_line = sentence.region.stop.line
        for line in range(start_line, stop_line + 1):
            if line in self.line_map:
                self.line_map[line].append(sentence)
            else:
                self.line_map[line] = [sentence]

    def remove_by_index(self, index_slice):
        '''Remove the sentences in self.sentence[start:end].'''
        for sentence in self.sentences[index_slice]:
            del self.state_id_map[sentence.state_id]
            start_line = sentence.region.start.line
            stop_line = sentence.region.stop.line
            for line in range(start_line, stop_line + 1):
                self.line_map[line].remove(sentence)
        del self.sentences[index_slice]

    def clear(self):
        '''Remove all the sentences.'''
        self.sentences.clear()
        self.state_id_map.clear()
        self.line_map.clear()

    def iter_covering_line(self, line):
        '''Iterate over the sentences that cover `line`.'''
        return self.line_map.get(line, [])

    def iter_starting_line(self, line):
        '''Iterate over the sentences that start from `line.`'''
        for sentence in self.line_map.get(line, []):
            if sentence.region.start.line == line:
                yield sentence

    def remove_covering_line(self, line):
        '''Remove the sentences that cover `line`.'''
        for sentence in self.line_map.get(line, []):
            del self.state_id_map[sentence.state_id]
            self.sentences.remove(sentence)
        self.line_map[line].clear()

    def __len__(self):
        return len(self.sentences)

    def __bool__(self):
        return bool(self.sentences)


class STM:
    '''A STM records the execution state of a Coq document.

    It cares about only forwarding and backwarding.
    '''

    def __init__(self, bufnr):
        '''Create a empty state machine.

        `bufnr` is the number of the related buffer. It never directly uses `bufnr`, only as an
        identifier to make UI actions.'''
        self._doc = _Document()
        self._failed_sentence = None
        self._init_state_id = None
        self._task_thread = _SequentialTaskThread()
        self._bufnr = bufnr
        self._tip_sentence = None

    def make_feedback_handler(self, handle_action):
        '''Return an associated feedback handler.'''
        return _FeedbackHandler(self._doc.state_id_map, handle_action)

    def init(self, call_async, handle_action):
        '''Initialize the state machine.'''
        def task(done):
            '''Send Init call.'''
            @_finally_call(done)
            def on_res(xml):
                '''Process the Init response.'''
                res = xp.InitRes.from_xml(xml)
                if not res.error:
                    self._init_state_id = res.init_state_id
                else:
                    raise RuntimeError(res.error.message)
            call_async(xp.InitReq(), on_res, self._make_on_lost_cb(done, handle_action))
        self._task_thread.schedule(task, 'init')

    def forward(self, sregion, call_async, handle_action):
        '''Forward one sentence.'''
        if self._doc and self._doc.sentences[-1].has_error():
            handle_action(actions.ShowMessage('Fix the error first', 'error'))
            return
        self._forward_one(sregion, call_async, handle_action)
        self._update_goal(call_async, handle_action)

    def forward_many(self, sregions, call_async, handle_action):
        '''The bulk call of `forward`.'''
        if self._doc and self._doc.sentences[-1].has_error():
            handle_action(actions.ShowMessage('Fix the error first.', 'error'))
            return
        for sregion in sregions:
            self._forward_one(sregion, call_async, handle_action)
        self._update_goal(call_async, handle_action)

    def backward(self, call_async, handle_action):
        '''Go backward to the previous state.'''
        def task(done):
            '''Go backward to the previous state of the current state.'''
            if self._doc:
                last = len(self._doc.sentences) - 1
                self._backward_before_index(last, done, call_async, handle_action)
            else:
                done()
        self._task_thread.schedule(task, 'backward')
        self._update_goal(call_async, handle_action)

    def backward_before_mark(self, mark, call_async, handle_action):
        '''Go backward to the sentence before the given mark.'''
        def task(done):
            '''Find the sentence and go backward.'''
            for i, sentence in enumerate(self._doc.sentences):
                stop = sentence.region.stop
                if stop.line > mark.line or \
                        (stop.line == mark.line and stop.col > mark.col):
                    self._backward_before_index(i, done, call_async, handle_action)
                    break
            else:
                done()
        self._task_thread.schedule(task, 'backward_before_mark')
        self._update_goal(call_async, handle_action)

    def get_last_stop(self):
        '''Return the stop of the last sentence.'''
        if self._doc:
            return self._doc.sentences[-1].region.stop
        return Mark(1, 1)

    def get_tip_stop(self):
        '''Return the stop of the tip sentence.'''
        if self._tip_sentence:
            return self._tip_sentence.region.stop
        if self._doc:
            return self._doc.sentences[-1].region.stop
        return Mark(1, 1)

    def is_busy(self):
        '''Return True if there is scheduled tasks.'''
        count = self._task_thread.scheduled_task_count()
        logger.debug('STM scheduled task count: %s', count)
        return count > 0

    def close(self, handle_action):
        '''Close and clean up the state machine.'''
        self._task_thread.shutdown_join()
        for sentence in self._doc.sentences:
            sentence.unhighlight(handle_action)
        self._doc.clear()

    def on_text_changed(self, changes, call_async, handle_action):
        '''Process the text changes by invalidating some states and adjusting the locations of
        other.'''
        return
        def _invalidate(done, sentence):
            '''Invalidate the `sentence`.'''
            self._backward_before_index(sentence)

        for line, changed, delta in changes:
            if changed:
                for sentence in self._doc.iter_covering_line(line):
                    sentence.unhighlight()
                self._doc.remove_covering_line(line)
            elif delta:
                for sentence in self._doc.iter_starting_line(line):
                    old_start_line = sentence.region.start.line
                    new_start_line = old_start_line + delta
                    start_col = sentence.region.start.col
                    sentence.region = sentence.region._replace(
                        start=Mark(new_start_line, start_col))
                    sentence.rehighlight(handle_action)

    def _forward_one(self, sregion, call_async, handle_action):
        '''Accept a new sentence region and go forward.'''
        def task(done):
            '''Send Add call.'''
            @_finally_call(done)
            def on_res(xml):
                '''Process the Add response.'''
                res = xp.AddRes.from_xml(xml)
                if not res.error:
                    state_id = res.new_state_id
                    sentence = Sentence(sregion, state_id)
                    self._doc.add(sentence, self._tip_sentence)
                    sentence.set_processing(handle_action)
                    handle_action(actions.ShowMessage(res.message, 'info'))

                    if res.next_state_id:
                        # Set the tip to new_state_id
                        self._tip_sentence = self._doc.state_id_map[res.next_state_id]
                    else:
                        self._tip_sentence = None
                else:
                    self._task_thread.discard_scheduled_tasks()
                    sentence = Sentence(sregion, 0)
                    self._failed_sentence = sentence
                    sentence.set_error(res.error.location, res.error.message, handle_action)

            self._clear_failed_sentence(handle_action)
            req = xp.AddReq(sregion.command, -1, self._tip_state_id(), False)
            call_async(req, on_res, self._make_on_lost_cb(done, handle_action))
        self._task_thread.schedule(task, 'forward_one')

    def _clear_failed_sentence(self, handle_action):
        '''Clear the highlighting of the last sentence that cannot be added.'''
        if self._failed_sentence:
            self._failed_sentence.unhighlight(handle_action)
            self._failed_sentence = None

    def _tip_state_id(self):
        '''Return the tip state id.'''
        if self._tip_sentence:
            return self._tip_sentence.state_id
        if self._doc:
            return self._doc.sentences[-1].state_id
        return self._init_state_id

    def _backward_before_index(self, index, done, call_async, handle_action):
        '''Go backward to the state of `self._doc.sentences[index - 1]`.

        If `index == 0`, go to the initial state.
        '''
        def on_res(xml):
            '''Process EditAt response and remove the trailing sentences.'''
            res = xp.EditAtRes.from_xml(xml)
            if not res.error:
                if not res.focused:
                    for sentence in self._doc.sentences[index:]:
                        sentence.unhighlight(handle_action)
                    self._doc.remove_by_index(slice(index, None))
                    self._tip_sentence = None
                else:
                    qed_sentence = self._doc.state_id_map[res.qed]
                    qed_index = self._doc.sentences.index(qed_sentence)
                    for sentence in self._doc.sentences[index:qed_index+1]:
                        sentence.unhighlight(handle_action)
                    self._doc.remove_by_index(slice(index, qed_index+1))
                    self._tip_sentence = self._doc.sentences[index - 1]
                done()
            else:
                # Coqtop will tell us which state is the nearest editable.
                # Backward to the editable state id.
                message = res.error.message
                handle_action(actions.ShowMessage(message, 'error'))

                sentence = self._doc.state_id_map[res.error.state_id]
                sindex = self._doc.sentences.index(sentence)
                self._backward_before_index(sindex + 1, done, call_async, handle_action)

        if index == 0:
            state_id = self._init_state_id
        else:
            state_id = self._doc.sentences[index - 1].state_id
        self._clear_failed_sentence(handle_action)
        req = xp.EditAtReq(state_id)
        call_async(req, on_res, self._make_on_lost_cb(done, handle_action))

    def _edit_at_current_state(self, call_async, handle_action):
        '''Let the Coqtop state machine edit at the current state.

        Used when closing a proof with Qed.
        '''
        def task(done):
            '''The task.'''
            self._backward_before_index(len(self._doc), done, call_async, handle_action)
        self._task_thread.schedule(task, 'edit_at_current_state')

    def _update_goal(self, call_async, handle_action):
        '''Update the goals.'''
        def task(done):
            '''Send Goal call.'''
            @_finally_call(done)
            def on_res(xml):
                '''Process the Goal response.'''
                res = xp.GoalRes.from_xml(xml)
                if not res.error:
                    handle_action(actions.ShowGoals(res.goals))
            call_async(xp.GoalReq(), on_res, self._make_on_lost_cb(done, handle_action))
        self._task_thread.schedule(task, 'get_goals')

    def _make_on_lost_cb(self, done, handle_action):
        '''Return an on-connection-lost callback that notifies the UI and discards all the schedule
        tasks.'''
        @_finally_call(done)
        def callback():
            '''Process the connection lost event.'''
            self._task_thread.discard_scheduled_tasks()
            for sentence in self._doc.sentences:
                sentence.unhighlight(handle_action)
            self._doc.clear()
            handle_action(actions.ConnLost(self._bufnr))
        return callback
