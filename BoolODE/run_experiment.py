#!/usr/bin/env python
# coding: utf-8
__author__ = 'Amogh Jalihal'
import os
import sys
import ast
import time
import warnings
import numpy as np
import pandas as pd
from tqdm import tqdm
from pathlib import Path
from optparse import OptionParser
from itertools import combinations
from scipy.integrate import odeint
from sklearn.cluster import KMeans
from importlib.machinery import SourceFileLoader
import multiprocessing as mp
# local imports
from BoolODE import utils
from BoolODE.model_generator import GenerateModel
from BoolODE import simulator 

np.seterr(all='raise')

def Experiment(mg, Model,
               tspan,
               settings,
               icsDF,
               writeProtein=False,
               normalizeTrajectory=False):
    """
    Carry out an `in-silico` experiment. This function takes as input 
    an ODE model defined as a python function and carries out stochastic
    simulations. BoolODE defines a _cell_ as a single time point from 
    a simulated time course. Thus, in order to obtain 50 single cells,
    BoolODE carries out 50 simulations, which are stored in ./simulations/.
    Further, if it is known that the resulting single cell dataset will
    exhibit multiple trajectories, the user can specify  the number of clusters in
    `nClusters`; BoolODE will then cluster the entire simulation, such that each
    simulated trajectory possesess a cluster ID.

    :param mg: Model details obtained by instantiating an object of GenerateModel
    :type mg: BoolODE.GenerateModel
    :param Model: Function defining ODE model
    :type Model: function
    :param tspan: Array of time points
    :type tspan: ndarray
    :param settings: The job settings dictionary
    :type settings: dict
    :param icsDF: Dataframe specifying initial condition for simulation
    :type icsDF: pandas DataFrame
    :param writeProtein: Bool specifying if the protein values should be written to file. Default = False
    :type writeProtein: bool
    :param normalizeTrajectory: Bool specifying if the gene expression values should be scaled between 0 and 1.
    :type normalizeTrajectory: bool 
    """
    ####################
    allParameters = dict(mg.ModelSpec['pars'])
    parNames = sorted(list(allParameters.keys()))
    ## Use default parameters 
    pars = [mg.ModelSpec['pars'][k] for k in parNames]
    ####################
    rnaIndex = [i for i in range(len(mg.varmapper.keys())) if 'x_' in mg.varmapper[i]]
    revvarmapper = {v:k for k,v in mg.varmapper.items()}
    proteinIndex = [i for i in range(len(mg.varmapper.keys())) if 'p_' in mg.varmapper[i]]

    y0 = [mg.ModelSpec['ics'][mg.varmapper[i]] for i in range(len(mg.varmapper.keys()))]
    ss = np.zeros(len(mg.varmapper.keys()))
    
    for i,k in mg.varmapper.items():
        if 'x_' in k:
            ss[i] = 1.0
        elif 'p_' in k:
            if k.replace('p_','') in mg.proteinlist:
                # Seting them to the threshold
                # causes them to drop to 0 rapidly
                # TODO: try setting to threshold < v < y_max
                ss[i] = 20.
            
    if not icsDF.empty:
        icsspec = icsDF.loc[0]
        genes = ast.literal_eval(icsspec['Genes'])
        values = ast.literal_eval(icsspec['Values'])
        icsmap = {g:v for g,v in zip(genes,values)}
        for i,k in mg.varmapper.items():
            for p in mg.proteinlist:
                if p in icsmap.keys():
                    ss[revvarmapper['p_'+p]] = icsmap[p]
                else:
                    ss[revvarmapper['p_'+p]] = 0.01
            for g in mg.genelist:
                if g in icsmap.keys():
                    ss[revvarmapper['x_'+g]] = icsmap[g]
                else:
                    ss[revvarmapper['x_'+g]] = 0.01
            
    if len(mg.proteinlist) == 0:
        result = pd.DataFrame(index=pd.Index([mg.varmapper[i] for i in rnaIndex]))
    else:
        speciesoi = [revvarmapper['p_' + p] for p in proteinlist]
        speciesoi.extend([revvarmapper['x_' + g] for g in mg.genelist])
        result = pd.DataFrame(index=pd.Index([mg.varmapper[i] for i in speciesoi]))


    n_snapshots = settings.get('n_snapshots', 0)
        
    # Index of every possible time point. Sample from this list
    startat = 0
    timeIndex = [i for i in range(startat, len(tspan))]        

    ## Construct dictionary of arguments to be passed
    ## to simulateAndSample(), done in parallel
    outPrefix = str(settings['outprefix'])
    argdict = {}
    argdict['mg'] = mg
    argdict['allParameters'] = allParameters
    argdict['parNames'] = parNames
    argdict['Model'] = Model
    argdict['tspan'] = tspan
    argdict['varmapper'] = mg.varmapper
    argdict['timeIndex'] = timeIndex
    argdict['genelist'] = mg.genelist
    argdict['proteinlist'] = mg.proteinlist
    argdict['writeProtein'] = writeProtein
    argdict['outPrefix'] = outPrefix
    argdict['sampleCells'] = settings['sample_cells'] # TODO consider removing this option
    argdict['pars'] = pars
    argdict['ss'] = ss
    argdict['ModelSpec'] = mg.ModelSpec
    argdict['rnaIndex'] = rnaIndex
    argdict['proteinIndex'] = proteinIndex
    argdict['revvarmapper'] = revvarmapper
    argdict['x_max'] = mg.kineticParameterDefaults['x_max']
    argdict['n_snapshots'] = n_snapshots

    if settings['sample_cells']:
        # pre-define the time points from which a cell will be sampled
        # per simulation
        sampleAt = np.random.choice(timeIndex, size=settings['num_cells'])
        header = ['E' + str(cellid) + '_' + str(time) \
                  for cellid, time in\
                  zip(range(settings['num_cells']), sampleAt)]
        
        argdict['header'] = header
    else:
        # initialize dictionary to hold raveled values, used to cluster
        # This will be useful later.
        groupedDict = {}         

    simfilepath = Path(outPrefix, './simulations/')
    if not os.path.exists(simfilepath):
        print(simfilepath, "does not exist, creating it...")
        os.makedirs(simfilepath)

    print('n_snapshots =', n_snapshots)
    print('Starting simulations...')

    start = time.time()

    print('parallelize =', settings['doParallel'])
    if settings['doParallel']:
        with mp.Pool() as pool:
            jobs = []
            for cellid in range(settings['num_cells']):
                #print(f'Simulating cell {cellid}...')
                cell_args = dict(argdict, seed=cellid, cellid=cellid)
                job = pool.apply_async(simulateAndSample, args=(cell_args,))
                jobs.append(job)
                
            for job in jobs:
                job.get()
    else:
        for cellid in tqdm(range(settings['num_cells'])):
            print(f'Simulating cell {cellid}...')
            argdict['seed'] = cellid
            argdict['cellid'] = cellid
            simulateAndSample(argdict)

    print("Simulations took %0.3f s"%(time.time() - start))
    frames = []
    print('starting to concat files')
    start = time.time()

    for cellid in range(settings['num_cells']):
        csvPath = f'{outPrefix}/simulations/E{cellid}.csv'
        df = pd.read_csv(csvPath, index_col=0)
        df = df.sort_index()

        # In the standard, older workflow, we do a single timepoint -> we store raveled data for clustering
        # But if n_snapshots>0, we might have multiple columns from each cell.
        if n_snapshots == 0:
            groupedDict[f'E{cellid}'] = df.values.ravel()

        frames.append(df.T)  # store transposed for final concat

    stop = time.time()
    print("Concating files took %.2f s" %(stop-start))
    result = pd.concat(frames,axis=0)
    result = result.T
    indices = result.index
    newindices = [i.replace('x_','') for i in indices]
    result.index = pd.Index(newindices)
    
    if settings['nClusters'] > 1 and n_snapshots == 0:
        ## Carry out k-means clustering to identify which
        ## trajectory a simulation belongs to
        print('Starting k-means clustering')
        groupedDF = pd.DataFrame.from_dict(groupedDict)
        print('Clustering simulations...')
        start = time.time()            
        # Find clusters in the experiments
        clusterLabels= KMeans(n_clusters=settings['nClusters']).fit(groupedDF.T.values).labels_
        print('Clustering took %0.3fs' % (time.time() - start))
        clusterDF = pd.DataFrame(data=clusterLabels, index =\
                                 groupedDF.columns, columns=['cl'])
        clusterDF.to_csv(outPrefix + '/ClusterIds.csv')
    else:
        print(f'nClusters={settings.get("nClusters",1)} or n_snapshots={n_snapshots}, skipping k-means')

    ##################################################
    return result
    
def startRun(settings):
    """
    settings contain n_snapshots
    Start a simulation run. Loads model file, starts an Experiment(),
    and generates the appropriate input files
    """
    validInput = utils.checkValidModelDefinitionPath(settings['modelpath'], settings['name'])
    startfull = time.time()

    outdir = settings['outprefix']
    if not os.path.exists(outdir):
        print(outdir, "does not exist, creating it...")
        os.makedirs(outdir)
        
    ##########################################
    ## Read advanced model specification files
    ## If these are not specified, the dataFrame objects
    ## are left empty
    parameterInputsDF = utils.checkValidInputPath(settings['parameter_inputs_path'])
    parameterSetDF = utils.checkValidInputPath(settings['parameter_set'])
    icsDF = utils.checkValidInputPath(settings['icsPath'])
    interactionStrengthDF = utils.checkValidInputPath(settings['interaction_strengths'])

    speciesTypeDF = utils.checkValidInputPath(settings['species_type'])
    ##########################################

    # Simulator settings
    tmax = settings['simulation_time']    
    integration_step_size = settings['integration_step_size']
    tspan = np.linspace(0,tmax,int(tmax/integration_step_size))

    print(settings)

    # Generate the ODE model from the specified boolean model
    mg = GenerateModel(settings,
                       parameterInputsDF,
                       parameterSetDF,
                       interactionStrengthDF)
    genesDict = {}

    # Load the ODE model file
    model = SourceFileLoader("model", mg.path_to_ode_model.as_posix()).load_module()

    ## Function call - do the in silico experiment
    resultDF = Experiment(mg, model.Model,
                          tspan,
                          settings,
                          icsDF,
                          writeProtein=settings['writeProtein'],
                          normalizeTrajectory=settings['normalizeTrajectory'])
    
    # Write simulation output. Creates ground truth files.
    print('Generating input files for pipline...')
    start = time.time()
    utils.generateInputFiles(resultDF, mg.df,
                             mg.withoutRules,
                             parameterInputsDF,
                             tmax,
                             settings['num_cells'],
                             outPrefix=settings['outprefix'],
                             n_snapshots=settings.get('n_snapshots', 0))
    print('Input file generation took %0.2f s' % (time.time() - start))
    print("BoolODE.py took %0.2fs"% (time.time() - startfull))

def simulateAndSample(argdict):
    """
    Handles parallelization of ODE simulations.
    Calls the simulator with simulation settings.
    """
    mg = argdict['mg']
    allParameters = argdict['allParameters']
    parNames = argdict['parNames']
    Model = argdict['Model']
    tspan = argdict['tspan']
    varmapper = argdict['varmapper']
    timeIndex = argdict['timeIndex']
    genelist = argdict['genelist']
    proteinlist = argdict['proteinlist']
    writeProtein=argdict['writeProtein']
    cellid = argdict['cellid']
    outPrefix = argdict['outPrefix']
    sampleCells = argdict['sampleCells']
    ss = argdict['ss']
    ModelSpec = argdict['ModelSpec']
    rnaIndex = argdict['rnaIndex']
    proteinIndex = argdict['proteinIndex']
    genelist = argdict['genelist']
    proteinlist = argdict['proteinlist']
    revvarmapper = argdict['revvarmapper']
    seed = argdict['seed']
    pars = argdict['pars']
    x_max = argdict['x_max']
    
    # Retained for debugging
    isStochastic = True

    n_snapshots = argdict.get('n_snapshots', 0)
    
    if sampleCells:
        header = argdict['header']
        
    pars = {}
    for k, v in allParameters.items():
        pars[k] = v
    pars = [pars[k] for k in parNames]
    
    ## Boolean to check if a simulation is going to a
    ## 0 steady state, with all genes/proteins dying out
    retry = True
    tries = 0

    outPrefix = os.path.join(argdict['outPrefix'], 'simulations')
    os.makedirs(outPrefix, exist_ok=True)

    # ## timepoints
    # tps = [i for i in range(1,len(tspan))]
    # ## gene ids
    # gid = [i for i,n in varmapper.items() if 'x_' in n]

    #print("Do simulation for cell %d" % cellid)
    while retry:
        seed += 1000
        y0_exp = simulator.getInitialCondition(ss, ModelSpec, rnaIndex, proteinIndex,
                                     genelist, proteinlist,
                                     varmapper,revvarmapper)
        
        P = simulator.simulateModel(Model, y0_exp, pars, isStochastic, tspan, seed)
        P = P.T
        retry = False
        # 3) Check if everything died
        ## If the largest value of a protein achieved in a simulation is
        ## less than 10% of the y_max, drop the simulation.
        ## This check stems from the observation that in some simulations,
        ## all genes go to the 0 steady state in some rare simulations.
        maxExp = P[rnaIndex, :].max()
        if maxExp < 0.1 * x_max:
            retry = True
        tries += 1
    print("done")

    # If n_snapshots == 0, keep old approach: single-time approach
    if n_snapshots == 0:
        # We skip the first time index for historical reasons: tps = [1..end]
        timePoints = list(range(1, P.shape[1]))
        subset = P[rnaIndex, :][:, timePoints]
        colNames = [f'E{cellid}_{t}' for t in timePoints]
        df = pd.DataFrame(subset, index=genelist, columns=colNames)
        if sampleCells:
            ## Write a single cell to file
            ## These samples allow for quickly and
            ## reproducibly testing the output.
            sampledf = utils.sampleCellFromTraj(cellid,
                                            tspan, 
                                            P,
                                            varmapper, timeIndex,
                                            genelist, proteinlist,
                                            header,
                                            writeProtein=writeProtein)
            sampledf = sampledf.T
            sampledf.to_csv(outPrefix + 'E' + str(cellid) + '-cell.csv')            
            

    else:
        # n_snapshots > 0: pick snapshots evenly across entire trajectory
        # e.g. if P.shape[1] = 101 time steps, n_snapshots=5
        # sample around [0, 25, 50, 75, 100]
        steps = P.shape[1] - 1
        # snapshots indices are random between the indices
        snapshotIndicesLimits = np.round(np.linspace(0, steps, n_snapshots + 1)).astype(int)
        # Randomly select one timepoint in each snapshot interval
        snapshotIndices = [np.random.randint(snapshotIndicesLimits[i], snapshotIndicesLimits[i+1])
                       for i in range(n_snapshots)]
        # Logging for debugging
        
        print(f"Cell {cellid} snapshot indices:", snapshotIndices)

        colNames = [f'E{cellid}_t{i}' for i in range(n_snapshots)]
        subset = P[rnaIndex, :][:, snapshotIndices]
        df = pd.DataFrame(subset, index=genelist, columns=colNames)

    # 4) Save to CSV
    df.to_csv(os.path.join(outPrefix, f'E{cellid}.csv'))
    print("[Cell %d] Simulation complete." % cellid)

    
    # Debug prints if we had multiple tries
    if tries > 1:
        print(f'[Cell {cellid}] took {tries} tries to get non-zero expression.')
