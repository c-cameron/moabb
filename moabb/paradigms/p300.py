"""P300 Paradigms"""

import abc
import logging

import mne
import numpy as np
import pandas as pd

from moabb.datasets import utils
from moabb.datasets.fake import FakeDataset
from moabb.paradigms.base import BaseParadigm


log = logging.getLogger(__name__)


class BaseP300(BaseParadigm):
    """Base P300 paradigm.

    Please use one of the child classes

    Parameters
    ----------

    filters: list of list (defaults [[7, 35]])
        bank of bandpass filter to apply.

    events: List of str | None (default None)
        event to use for epoching. If None, default to all events defined in
        the dataset.

    tmin: float (default 0.0)
        Start time (in second) of the epoch, relative to the dataset specific
        task interval e.g. tmin = 1 would mean the epoch will start 1 second
        after the begining of the task as defined by the dataset.

    tmax: float | None, (default None)
        End time (in second) of the epoch, relative to the begining of the
        dataset specific task interval. tmax = 5 would mean the epoch will end
        5 second after the begining of the task as defined in the dataset. If
        None, use the dataset value.

    baseline: None | tuple of length 2
            The time interval to consider as “baseline” when applying baseline
            correction. If None, do not apply baseline correction.
            If a tuple (a, b), the interval is between a and b (in seconds),
            including the endpoints.
            Correction is applied by computing the mean of the baseline period
            and subtracting it from the data (see mne.Epochs)

    channels: list of str | None (default None)
        list of channel to select. If None, use all EEG channels available in
        the dataset.

    resample: float | None (default None)
        If not None, resample the eeg data with the sampling rate provided.
    """

    def __init__(
        self,
        filters=([1, 24],),
        events=None,
        tmin=0.0,
        tmax=None,
        reject_tmin=None,
        reject_tmax=None,
        baseline=None,
        channels=None,
        resample=None,
        reject_uv=None,
        reject_from_eog=False,
    ):
        super().__init__()
        self.filters = filters
        self.events = events
        self.channels = channels
        self.baseline = baseline
        self.resample = resample
        self.reject_uv = reject_uv
        self.reject_from_eog = reject_from_eog #Can be either be a string, kwarg dict for find_eog_events or True (uses dataset EOG channel

        if tmax is not None:
            if tmin >= tmax:
                raise (ValueError("tmax must be greater than tmin"))

        self.tmin = tmin
        self.tmax = tmax
        if reject_tmin is not None:
            if reject_tmin <= tmin:
                raise ValueError(f'reject_tmin must be greater or equal to tmin:{tmin}')
        if reject_tmax is not None:
            if reject_tmax <= tmin:
                raise ValueError(f'reject_tmax must be greater than tmin: {tmin}')
        if reject_tmin is not None and reject_tmax is not None:
            if reject_tmin >= reject_tmax:
                raise ValueError('reject_tmax must be greater than reject_tmin')
        self.reject_tmin = reject_tmin
        self.reject_tmax = reject_tmax

    def is_valid(self, dataset):
        ret = True
        if not (dataset.paradigm == "p300"):
            ret = False

        # check if dataset has required events
        if self.events:
            if not set(self.events) <= set(dataset.event_id.keys()):
                ret = False

        # we should verify list of channels, somehow
        return ret

    @abc.abstractmethod
    def used_events(self, dataset):
        pass

    def process_raw(self, raw, dataset, return_epochs=False, return_runs=False):
        # find the events, first check stim_channels then annotations
        
        if self.reject_from_eog:
            if isinstance(self.reject_from_eog, dict):
                #If its a dict, assuming its kwargs, no further checking.
                eog_kwargs = self.reject_from_eog
            elif isinstance(self.reject_from_eog, str):
                eog_kwargs = {"ch_name":self.reject_from_eog}
                if eog_kwargs["ch_name"] not in raw.ch_names:
                    raise ValueError(f"{eog_kwargs['ch_name']} not in channels ")
            elif self.reject_from_eog == True:
                eog_kwargs = {"ch_name":None}
            #EOG_CHANNEL = "EOGvu"

            time_pre_blink = 0.25
            blink_length = 0.7
            eog_events = mne.preprocessing.find_eog_events(raw, **eog_kwargs)
            onsets = eog_events[:, 0] / raw.info['sfreq'] - time_pre_blink
            durations = [blink_length] * len(eog_events)
            descriptions = ['bad blink'] * len(eog_events)
            blink_annot = mne.Annotations(onsets, durations, descriptions,
                                          orig_time=raw.info['meas_date'])
            raw.set_annotations(raw.annotations + blink_annot)

        stim_channels = mne.utils._get_stim_channel(None, raw.info, raise_error=False)
        if len(stim_channels) > 0:
            events = mne.find_events(raw, shortest_event=0, verbose=False)
        else:
            events, _ = mne.events_from_annotations(raw, verbose=False)

        # picks channels
        channels = () if self.channels is None else self.channels
        picks = mne.pick_types(raw.info, eeg=True, stim=False, include=channels)
        if self.channels is None:
            picks = mne.pick_types(raw.info, eeg=True, stim=False)
        else:
            picks = mne.pick_channels(
                raw.info["ch_names"], include=channels, ordered=True
            )

        # get event id
        event_id = self.used_events(dataset)

        # pick events, based on event_id
        try:
            if type(event_id["Target"]) is list and type(event_id["NonTarget"]) == list:
                event_id_new = dict(Target=1, NonTarget=0)
                events = mne.merge_events(events, event_id["Target"], 1)
                events = mne.merge_events(events, event_id["NonTarget"], 0)
                event_id = event_id_new
            events = mne.pick_events(events, include=list(event_id.values()))
        except RuntimeError:
            # skip raw if no event found
            return

        # get interval
        tmin = self.tmin + dataset.interval[0]
        if self.tmax is None:
            tmax = dataset.interval[1]
        else:
            tmax = self.tmax + dataset.interval[0]
        if self.reject_tmax is not None:
            if self.reject_tmax >= dataset.interval[1]:
                raise ValueError('reject_tmax needs to be shorter than tmax')
        X = []
        runs = []
        for bandpass in self.filters:
            fmin, fmax = bandpass
            # filter data
            raw_f = raw.copy().filter(
                fmin, fmax, method="iir", picks=picks, verbose=False
            )
            # epoch data
            baseline = self.baseline
            if baseline is not None:
                baseline = (
                    self.baseline[0] + dataset.interval[0],
                    self.baseline[1] + dataset.interval[0],
                )
                bmin = baseline[0] if baseline[0] < tmin else tmin
                bmax = baseline[1] if baseline[1] > tmax else tmax
            else:
                bmin = tmin
                bmax = tmax
            epoching_kwargs = dict(
                event_id=event_id,
                tmin=tmin,
                tmax=tmax,
                reject_tmin=self.reject_tmin,
                reject_tmax=self.reject_tmax,
                proj=False,
                baseline=baseline,
                preload=True,
                verbose=False,
                picks=picks,
                on_missing="ignore",
                reject_by_annotation=self.reject_from_eog
            )
            epochs = mne.Epochs(
                raw_f,
                events,
                **epoching_kwargs,
            )
            if self.reject_uv is not None:
                epochs.drop_bad(dict(eeg=self.reject_uv / 1e6))
            if bmin < tmin or bmax > tmax:
                epochs.crop(tmin=tmin, tmax=tmax)
            if self.resample is not None:
                epochs = epochs.resample(self.resample)
            # rescale to work with uV
            runs.append(raw_f if return_runs else None)
            if return_epochs:
                X.append(epochs)
            else:
                X.append(dataset.unit_factor * epochs.get_data())

        inv_events = {k: v for v, k in event_id.items()}
        labels = np.array([inv_events[e] for e in epochs.events[:, -1]])

        # if only one band, return a 3D array, otherwise return a 4D
        if not return_epochs:
            if len(self.filters) == 1:
                X = X[0]
            else:
                X = np.array(X).transpose((1, 2, 3, 0))

        metadata = pd.DataFrame(index=range(len(labels)))
        return X, labels, metadata, (runs, events, epoching_kwargs)

    @property
    def datasets(self):
        if self.tmax is None:
            interval = None
        else:
            interval = self.tmax - self.tmin
        return utils.dataset_search(
            paradigm="p300", events=self.events, interval=interval, has_all_events=True
        )

    @property
    def scoring(self):
        return "roc_auc"


class SinglePass(BaseP300):
    """Single Bandpass filter P300

    P300 paradigm with only one bandpass filter (default 1 to 24 Hz)

    Parameters
    ----------
    fmin: float (default 1)
        cutoff frequency (Hz) for the high pass filter

    fmax: float (default 24)
        cutoff frequency (Hz) for the low pass filter

    events: List of str | None (default None)
        event to use for epoching. If None, default to all events defined in
        the dataset.

    tmin: float (default 0.0)
        Start time (in second) of the epoch, relative to the dataset specific
        task interval e.g. tmin = 1 would mean the epoch will start 1 second
        after the begining of the task as defined by the dataset.

    tmax: float | None, (default None)
        End time (in second) of the epoch, relative to the begining of the
        dataset specific task interval. tmax = 5 would mean the epoch will end
        5 second after the begining of the task as defined in the dataset. If
        None, use the dataset value.

    baseline: None | tuple of length 2
            The time interval to consider as “baseline” when applying baseline
            correction. If None, do not apply baseline correction.
            If a tuple (a, b), the interval is between a and b (in seconds),
            including the endpoints.
            Correction is applied by computing the mean of the baseline period
            and subtracting it from the data (see mne.Epochs)

    channels: list of str | None (default None)
        list of channel to select. If None, use all EEG channels available in
        the dataset.

    resample: float | None (default None)
        If not None, resample the eeg data with the sampling rate provided.

    """

    def __init__(self, fmin=1, fmax=24, **kwargs):
        if "filters" in kwargs.keys():
            raise (ValueError("P300 does not take argument filters"))
        super().__init__(filters=[[fmin, fmax]], **kwargs)


class P300(SinglePass):
    """P300 for Target/NonTarget classification

    Metric is 'roc_auc'

    """

    def __init__(self, **kwargs):
        if "events" in kwargs.keys():
            raise (ValueError("P300 dont accept events"))
        super().__init__(events=["Target", "NonTarget"], **kwargs)

    def used_events(self, dataset):
        return {ev: dataset.event_id[ev] for ev in self.events}

    @property
    def scoring(self):
        return "roc_auc"


class FakeP300Paradigm(P300):
    """Fake P300 for Target/NonTarget classification."""

    @property
    def datasets(self):
        return [FakeDataset(["Target", "NonTarget"], paradigm="p300")] 
