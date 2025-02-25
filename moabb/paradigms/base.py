import logging
import os
import pickle
from abc import ABCMeta, abstractmethod, abstractproperty
from pathlib import Path

import mne
import numpy as np
import pandas as pd


log = logging.getLogger(__name__)


class BaseParadigm(metaclass=ABCMeta):
    """Base Paradigm."""

    def __init__(self):
        pass

    @abstractproperty
    def scoring(self):
        """Property that defines scoring metric (e.g. ROC-AUC or accuracy
        or f-score), given as a sklearn-compatible string or a compatible
        sklearn scorer.

        """
        pass

    @abstractproperty
    def datasets(self):
        """Property that define the list of compatible datasets"""
        pass

    @abstractmethod
    def is_valid(self, dataset):
        """Verify the dataset is compatible with the paradigm.

        This method is called to verify dataset is compatible with the
        paradigm.

        This method should raise an error if the dataset is not compatible
        with the paradigm. This is for example the case if the
        dataset is an ERP dataset for motor imagery paradigm, or if the
        dataset does not contain any of the required events.

        Parameters
        ----------
        dataset : dataset instance
            The dataset to verify.
        """
        pass

    def prepare_process(self, dataset):
        """Prepare processing of raw files

        This function allows to set parameter of the paradigm class prior to
        the preprocessing (process_raw). Does nothing by default and could be
        overloaded if needed.

        Parameters
        ----------

        dataset : dataset instance
            The dataset corresponding to the raw file. mainly use to access
            dataset specific information.
        """
        pass

    def process_raw(
        self, raw, dataset, return_epochs=False, return_runs=False
    ):  # noqa: C901
        """
        Process one raw data file.

        This function apply the preprocessing and eventual epoching on the
        individual run, and return the data, labels and a dataframe with
        metadata.

        metadata is a dataframe with as many row as the length of the data
        and labels.

        Parameters
        ----------
        raw: mne.Raw instance
            the raw EEG data.
        dataset : dataset instance
            The dataset corresponding to the raw file. mainly use to access
            dataset specific information.
        return_epochs: boolean
            This flag specifies whether to return only the data array or the
            complete processed mne.Epochs

        returns
        -------
        X : Union[np.ndarray, mne.Epochs]
            the data that will be used as features for the model
            Note: if return_epochs=True,  this is mne.Epochs
                  if return_epochs=False, this is np.ndarray
        labels: np.ndarray
            the labels for training / evaluating the model
        metadata: pd.DataFrame
            A dataframe containing the metadata

        """
        # get events id
        event_id = self.used_events(dataset)

        # find the events, first check stim_channels then annotations
        stim_channels = mne.utils._get_stim_channel(None, raw.info, raise_error=False)
        if len(stim_channels) > 0:
            events = mne.find_events(raw, shortest_event=0, verbose=False)
        else:
            try:
                events, _ = mne.events_from_annotations(
                    raw, event_id=event_id, verbose=False
                )
            except ValueError:
                log.warning("No matching annotations in {}".format(raw.filenames))
                return

        # picks channels
        if self.channels is None:
            picks = mne.pick_types(raw.info, eeg=True, stim=False)
        else:
            picks = mne.pick_channels(
                raw.info["ch_names"], include=self.channels, ordered=True
            )

        # pick events, based on event_id
        try:
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

        X = []
        runs = []
        for bandpass in self.filters:
            fmin, fmax = bandpass
            # filter data
            #heres 1 proble with too much data.
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
            epochs = mne.Epochs(
                raw_f,
                events,
                event_id=event_id,
                tmin=bmin,
                tmax=bmax,
                proj=False,
                baseline=baseline,
                preload=True,
                verbose=False,
                picks=picks,
                event_repeated="drop",
                on_missing="ignore",
            )
            if bmin < tmin or bmax > tmax:
                epochs.crop(tmin=tmin, tmax=tmax)
            if self.resample is not None:
                epochs = epochs.resample(self.resample)
            # rescale to work with uV
            if return_epochs:
                X.append(epochs)
            else:
                X.append(dataset.unit_factor * epochs.get_data())
            if return_runs:
                runs.append(raw_f)

        inv_events = {k: v for v, k in event_id.items()}
        labels = np.array([inv_events[e] for e in epochs.events[:, -1]])

        # if only one band, return a 3D array, otherwise return a 4D
        if len(self.filters) == 1:
            X = X[0]
        else:
            X = np.array(X).transpose((1, 2, 3, 0))

        metadata = pd.DataFrame(index=range(len(labels)))
        return X, labels, metadata, runs

    def get_data(
        self, dataset, subjects=None, return_epochs=False, return_runs=False, cache=False
    ):
        """
        Return the data for a list of subject.

        return the data, labels and a dataframe with metadata. the dataframe
        will contain at least the following columns

        - subject : the subject indice
        - session : the session indice
        - run : the run indice

        parameters
        ----------
        dataset:
            A dataset instance.
        subjects: List of int
            List of subject number
        return_epochs: boolean
            This flag specifies whether to return only the data array or the
            complete processed mne.Epochs
        return_runs: boolean
            If True, the processed runs before epoching are also returned
        cache: boolean
            If True, paradigm processed data is stored in /tmp and read from
            there if available. WARNING: does not notice changes in preprocessing
            and could lead to disk space issues.

        returns
        -------
        X : Union[np.ndarray, mne.Epochs]
            the data that will be used as features for the model
            Note: if return_epochs=True,  this is mne.Epochs
                  if return_epochs=False, this is np.ndarray
        labels: np.ndarray
            the labels for training / evaluating the model
        metadata: pd.DataFrame
            A dataframe containing the metadata.
        """

        if cache:
            tmp = Path("/tmp/moabb/cache")
            os.makedirs(tmp, exist_ok=True)
            prefix = f"{dataset.__class__.__name__}_{subjects}"
            try:
                with open(tmp / f"{prefix}.pkl", "rb") as pklf:
                    d = pickle.load(pklf)
                labels = d["labels"]
                metadata = d["metadata"]
                X = d["X"]
                raws = None
                print("Using cached data: Beware that it might not be the data you want!")
                return X, labels, metadata, raws
            except Exception as e:
                print("Could not read cached data. Preprocessing from scratch.")
                print(e)

        if not self.is_valid(dataset):
            message = "Dataset {} is not valid for paradigm".format(dataset.code)
            raise AssertionError(message)

        data = dataset.get_data(subjects)
        self.prepare_process(dataset)

        X = []
        labels = []
        metadata = []
        processed_runs = []
        for subject, sessions in data.items():
            for session, runs in sessions.items():
                for run, raw in runs.items():
                    proc = self.process_raw(
                        raw, dataset, return_epochs=return_epochs, return_runs=return_runs
                    )

                    if proc is None:
                        # this mean the run did not contain any selected event
                        # go to next
                        continue

                    x, lbs, met, praw = proc
                    met["subject"] = subject
                    met["session"] = session
                    met["run"] = run

                    # grow X and labels in a memory efficient way. can be slow
                    if len(x[0]) > 0:
                        metadata.append(met)
                        if len(X) > 0:
                            if return_epochs:
                                X.append(x)
                            else:
                                X = np.append(X, x, axis=0)
                            labels = np.append(labels, lbs, axis=0)
                        else:
                            X = [x] if return_epochs else x
                            labels = lbs
                        if return_runs:
                            processed_runs.append(praw)
                    else:
                        print(f"All epochs were removed in run {run}. Are you sure this is right?")

        metadata = pd.concat(metadata, ignore_index=True)
        if return_epochs:
            # TODO: how do we handle filter-bank for ERP? Should we at all?
            if type(X[0]) is list:
                X = [x[0] for x in X]
            X = mne.concatenate_epochs(X)

        if cache:
            tmp = Path("/tmp/moabb/cache")
            os.makedirs(tmp, exist_ok=True)
            prefix = f"{dataset.__class__.__name__}_{subjects}"
            try:
                # epochs.save(tmp / f"{prefix}-epo.fif", overwrite=True)
                with open(tmp / f"{prefix}.pkl", "wb") as pklf:
                    pickle.dump(
                        {
                            "labels": labels,
                            "metadata": metadata,
                            "X": X,
                        },
                        pklf,
                    )
            except Exception as e:
                print("Could not store cached data")
                print(e)

        return X, labels, metadata, processed_runs
