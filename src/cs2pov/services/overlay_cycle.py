"""Observe the public overlay while paused, then prove it was hidden again.

An empty tree proves no recording state. It can only authorize opening the
locally verified overlay. Every visibility input is consumed before delivery.
"""
from dataclasses import replace, asdict
import uuid

from cs2pov.adapters.nvidia_status import ObservationRequest, classify_snapshot
from cs2pov.storage.settings import DataError


class OverlayCycle:
    def __init__(self, monitor, adapter, *, guard, checkpoint, clock):
        self.monitor, self.adapter = monitor, adapter
        self.guard, self.checkpoint, self.clock = guard, checkpoint, clock
        self.active = False
        self.failed = False
        self.sequence = 0
        self.actions = []
        self.observations = []
        self.epoch = 0

    @staticmethod
    def closed(evidence):
        snapshot = evidence.snapshot
        if not snapshot or not snapshot.complete or snapshot.reason or len(snapshot.windows) != 1:
            return False
        window = snapshot.windows[0]
        single = (window.visible is True and len(window.nodes) == 1
                and window.nodes[0].parent is None and window.nodes[0].name == ''
                and window.nodes[0].control_type == 50032
                and window.nodes[0].enabled is True and window.nodes[0].offscreen is False
                and window.nodes[0].process_id == window.identity.pid)
        # Exact empty CEF scaffolding observed on the same locally verified
        # provider build after a cold start. No visible Button or state label.
        # Like the single-root case this only authorizes a visibility cycle.
        scaffold = ((None,50032,False,True),(0,50033,False,False),
                    (1,50030,False,True),(2,50026,False,True),
                    (3,50026,False,True),(4,50026,True,True),
                    (5,50020,True,True),(4,50026,False,True),
                    (3,50026,True,True))
        return single or (window.visible is True and len(window.nodes)==len(scaffold)
            and all(n.name=='' and n.process_id==window.identity.pid
                    and (n.parent,n.control_type,n.offscreen,n.enabled)==row
                    for n,row in zip(window.nodes,scaffold)))

    def _check(self):
        if (not self.active or self.failed or self.clock() >= self.deadline
                or self.guard() is not True):
            raise DataError('浮窗核验的任务、身份或原期限已失效，未重试输入。')

    def _valid(self, evidence, request):
        self._check()
        snapshot = evidence.snapshot
        if (evidence.request_id != request.request_id or snapshot is None
                or snapshot.request_id != request.request_id
                or not request.requested_at <= snapshot.started_at <= snapshot.observed_at <= self.clock()
                or self.clock()-snapshot.observed_at > 2
                or not snapshot.complete or snapshot.reason
                or len(snapshot.windows) != 1
                or snapshot.windows[0].identity != self.identity):
            raise DataError('浮窗状态未知、过期或来源不一致：' + evidence.reason[:80])

    def _toggle(self, direction, evidence):
        self._check()
        self.sequence += 1
        self.actions.append(dict(sequence=self.sequence, cycle=self.epoch,
            direction=direction, request_id=evidence.request_id,
            consumed_at=self.clock(), deadline=self.deadline))
        if len(self.actions) > 32:
            raise DataError('浮窗操作达到本轮限额。')
        self.checkpoint(dict(schema=1, actions=list(self.actions), observations=list(self.observations)))
        self._check()
        self.adapter.send_once(direction, self.identity, deadline=self.deadline,
                               input_guard=self.guard)
        self._check()

    def start(self, request, callback, *, identity, deadline):
        if self.active or self.failed or self.monitor.busy:
            return False
        self.epoch += 1
        self.active = True
        self.request, self.callback = request, callback
        self.identity, self.deadline = identity, deadline
        self.original = None
        self.phase = 'initial'
        try:
            self._check()
            self._read(replace(request, request_id=uuid.uuid4().hex, requested_at=self.clock()))
        except Exception as error:
            self._fail(error)
        return True

    def _read(self, request):
        epoch = self.epoch
        def result(evidence):
            if not self.active or epoch != self.epoch:
                return
            try:
                snapshot = evidence.snapshot
                self.observations.append(dict(phase=self.phase,request_id=request.request_id,
                    requested_at=request.requested_at,returned_at=self.clock(),state=evidence.state,
                    reason=evidence.reason[:80],snapshot_reason=snapshot.reason[:80] if snapshot else None,
                    started_at=snapshot.started_at if snapshot else None,
                    observed_at=snapshot.observed_at if snapshot else None,
                    windows=[dict(pid=w.identity.pid,created=w.identity.created,nodes=len(w.nodes))
                             for w in snapshot.windows] if snapshot else []))
                if len(self.observations)>96: raise DataError('本轮浮窗观察达到限额。')
                self.checkpoint(dict(schema=1,actions=list(self.actions),observations=list(self.observations)))
                self._valid(evidence, request)
                if self.phase == 'initial':
                    if self.closed(evidence):
                        self._toggle('open', evidence)
                        self.phase = 'opened'
                        self._read(self.request)
                        return
                    if evidence.state not in ('idle', 'recording'):
                        raise DataError('浮窗页面无法证明录制状态，未切换录制热键。')
                    self.phase = 'opened'
                    self._read(self.request)
                    return
                if self.phase in ('initial', 'opened'):
                    if evidence.state not in ('idle', 'recording'):
                        raise DataError('打开浮窗后仍未获得明确录制状态。')
                    self.original = evidence
                    self._toggle('close', evidence)
                    self.phase = 'closed'
                    close_request = replace(self.request, request_id=uuid.uuid4().hex,
                                            requested_at=self.clock())
                    self._read(close_request)
                    return
                if not self.closed(evidence):
                    raise DataError('浮窗收起未确认，禁止播放或录制。')
                if self.adapter.foreground_pid() != self.adapter.expected_identity.pid:
                    raise DataError('收起浮窗后受管理游戏未在前台。')
                self._check()
                if self.clock()-self.original.observed_at > 2:
                    raise DataError('录制状态已失鲜，未重复开关浮窗。')
                callback = self.callback
                original = self.original
                self.active = False
                callback(original)
            except Exception as error:
                self._fail(error)
        if self.monitor.start(request, result) is not True:
            raise DataError('浮窗只读工作进程未启动。')

    def _fail(self, error):
        from cs2pov.adapters.nvidia_status import NvidiaStatusEvidence
        callback = self.callback
        self.active = False
        self.failed = True
        callback(NvidiaStatusEvidence('unknown', str(error), self.request.request_id))

    def cancel(self):
        self.active = False
        self.failed = True
        self.epoch += 1
        self.monitor.cancel()
