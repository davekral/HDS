"""
Minimální, soběstačná implementace EventEmitteru.

Slouží jako záloha, když není k dispozici balíček ``event_emitter`` z materiálů
předmětu (viz import v dialog.py). Pokrývá rozhraní, které framework SpeechCloud
používá: ``on``, ``once``, ``emit`` (s předáním ``**kwargs`` posluchačům).

Úložiště posluchačů se inicializuje líně, takže třída funguje i jako mixin
vedle tornado.web.WebSocketHandler, jehož ``__init__`` náš ``__init__`` nevolá.
"""

from collections import defaultdict


class EventEmitter:
    def _ee(self):
        store = self.__dict__.get("_ee_listeners")
        if store is None:
            store = self.__dict__["_ee_listeners"] = defaultdict(list)
        return store

    def on(self, event, listener=None):
        if listener is None:
            def _decorator(func):
                self._ee()[event].append((func, False))
                return func
            return _decorator
        self._ee()[event].append((listener, False))
        return listener

    def once(self, event, listener=None):
        if listener is None:
            def _decorator(func):
                self._ee()[event].append((func, True))
                return func
            return _decorator
        self._ee()[event].append((listener, True))
        return listener

    def emit(self, event, *args, **kwargs):
        listeners = self._ee().get(event)
        if not listeners:
            return False
        snapshot = list(listeners)
        # Posluchače typu "once" z evidence odstraníme ještě před voláním,
        # aby se případná nová registrace během volání nezrušila.
        self._ee()[event] = [(f, once) for (f, once) in listeners if not once]
        for func, _once in snapshot:
            func(*args, **kwargs)
        return True

    def remove_listener(self, event, listener):
        kept = [(f, o) for (f, o) in self._ee().get(event, []) if f is not listener]
        self._ee()[event] = kept

    def remove_all_listeners(self, event=None):
        if event is None:
            self._ee().clear()
        else:
            self._ee().pop(event, None)
