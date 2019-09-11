# Licensed under the MIT license:
# http://www.opensource.org/licenses/mit-license.php

from __future__ import absolute_import, division, print_function
import numpy as np
import sys
import os
import time
import tempfile
import warnings
import logging

from six import string_types

from collections.abc import Iterable

import simtk.openmm as openmm
import simtk.unit as units

from . import forces

nm = units.meter * 1e-9
fs = units.second * 1e-15
ps = units.second * 1e-12

logging.basicConfig(level=logging.INFO)

class IntegrationFailError(Exception):
    pass


class EKExceedsError(Exception):
    pass 


class Simulation():

    def __init__(self, **kwargs):
        """Base class for openmm simulations

        All parameters here are floats. Units specified in a parameter. 

        Parameters
        ----------
        
        N : int
            number of particles 
        
        error_tol : float, optional
            Error tolerance parameter for variableLangevin integrator
            Values of around 0.01 are reasonable for a "nice" simulation
            (i.e. simulation with soft forces etc). 
            Simulations with strong forces may need 0.001 or less
            OpenMM manual recommends 0.001, but our forces tend to be "softer" than theirs


        timestep : number
            timestep in femtoseconds. Mandatory for non-variable integrators.
            Ignored for variableLangevin integrator. Value of 70-80 are appropriate

        collision_rate : number
            collision rate in inverse picoseconds. values of 0.01 or 0.05 are often used. 
            Consult with lab members on values. 
            
            In brief, equilibrium simulations likely do not care about the exact dynamics 
            you're using, and therefore can be simulated in a "ballistic" dynamics with 
            col_rate of around 0.001-0.01. 
            
            Dynamical simulations and active simulations may be more sensitive to col_rate,
            though this is still under discussion/investigation. 
            
            Johannes converged on using 0.1 for loop extrusion simulations, just to be safe. 


        PBCbox : (float,float,float) or False; default:False
            Controls periodic boundary conditions
            If PBCbox is False, do not use periodic boundary conditions
            If intending to use PBC, then set PBCbox to (x,y,z) where x,y,z are dimensions
            of the bounding box for PBC

        GPU : GPU index as a string ("0" for first, "1" for second etc.) 
            Machines with 1 GPU automatically select their GPU.

        integrator : "langevin", "variableLangevin", "verlet", "variableVerlet",
                     "brownian", optional Integrator to use
                     (see Openmm class reference)
                     
        mass : number or np.array
            Particle mass (default 100 amu)
            

        temperature : simtk.units.quantity(units.kelvin), optional
            Temperature of the simulation. Devault value is 300 K.

        verbose : bool, optional
            If True, prints a lot of stuff in the command line.

        length_scale : float, optional
            The geometric scaling factor of the system.
            By default, length_scale=1.0 and harmonic bonds and repulsive
            forces have the scale of 1 nm.

        max_Ek: float, optional
            raise error if kinetic energy in (kT/particle) exceeds this value 

        platform : string, optional
            Platform to use: 
            CUDA (preferred fast GPU platform)
            OpenCL (maybe slower GPU platofrm, does not need CUDA installed)
            CPU (medium speed parallelized CPU platform) 
            reference (slow CPU platform for debug)

        verbose : bool, optional
            Shout out loud about every change.

        
        precision: str, optional (not recommended to change)
            mixed is optimal for most situations. 
            If you are using double precision, it will be slower by a factor of 10 or so. 
        


        """
        default_args = {"platform":"CUDA", 
                       "GPU":"0",
                       "integrator":"variablelangevin", 
                       "temperature":300,
                       "PBCbox":False,
                       "length_scale":1.0,
                       "mass":100, 
                       "reporters":[],
                       "max_Ek":10 , 
                       "precision":"mixed", 
                       "verbose":False}
        valid_names = list(default_args.keys()) + ["N", "error_tol", "collision_rate", "timestep"]
        for i in kwargs.keys():
            if i not in valid_names:
                raise ValueError("incorrect argument provided: {0}. Allowed are {1}".format(i, valid_names))
        default_args.update(kwargs)
        kwargs = default_args
        self.kwargs = kwargs

        platform = kwargs["platform"]
        self.GPU = kwargs["GPU"]  # setting default GPU

        properties = {}
        if self.GPU.lower() != "default":
            if platform.lower() in ["cuda", "opencl"]:
                properties["DeviceIndex"] = str(self.GPU)
                properties["Precision"] = kwargs["precision"]
        self.properties = properties

        if platform.lower() == "opencl":
            platform_object = openmm.Platform.getPlatformByName('OpenCL')
        elif platform.lower() == "reference":
            platform_object = openmm.Platform.getPlatformByName('Reference')
        elif platform.lower() == "cuda":
            platform_object = openmm.Platform.getPlatformByName('CUDA')
        elif platform.lower() == "cpu":
            platform_object = openmm.Platform.getPlatformByName('CPU')
        else:
            raise RuntimeError("Undefined platform: {0}".format(platform))
        self.platform = platform_object
        
        self.temperature = kwargs["temperature"]

        self.collisionRate = kwargs["collision_rate"] * (1 / ps)

        self.integrator_type = kwargs["integrator"]                
        if isinstance(self.integrator_type, string_types):
            self.integrator_type = str(self.integrator_type)
            if self.integrator_type.lower() == "langevin":
                self.integrator = openmm.LangevinIntegrator(self.temperature,
                    kwargs["collision_rate"] * (1 / ps), kwargs["timestep"]* fs)
            elif self.integrator_type.lower() == "variablelangevin":
                self.integrator = openmm.VariableLangevinIntegrator(self.temperature,
                    kwargs["collision_rate"] * (1 / ps), kwargs["error_tol"])
            elif self.integrator_type.lower() == "verlet":
                self.integrator = openmm.VariableVerletIntegrator(kwargs["timestep"]* fs)
            elif self.integrator_type.lower() == "variableverlet":
                self.integrator = openmm.VariableVerletIntegrator(kwargs["error_tol"])

            elif self.integrator_type.lower() == 'brownian':
                self.integrator = openmm.BrownianIntegrator(self.temperature,
                   kwarg["collision_rate"] * (1 / ps), kwargs["timestep"])
            else:
                logging.info(
                    'Using the provided integrator object'
                       #'please select from "langevin", "variablelangevin", '
                       #'"verlet", "variableVerlet", '
                       #'"brownian" or provide an integrator object'
                )
                self.integrator = integrator
        else:
            self.integrator = self.integrator_type
            self.integrator_type = "UserDefined"
            kwargs["integrator"] = "user_defined"
        
        self.N = kwargs["N"]

        self.verbose = kwargs["verbose"]
        self.reporters = kwargs["reporters"]
        self.forces_applied = False
        self.length_scale = kwargs["length_scale"]
        self.eK_critical = kwargs["max_Ek"]  # Max allowed kinetic energy

        self.step = 0
        self.block = 0
        self.time = 0 

        self.nm = nm
        self.kB = units.BOLTZMANN_CONSTANT_kB * \
            units.AVOGADRO_CONSTANT_NA  # Boltzmann constant
        self.kT = self.kB * self.temperature * units.kelvin  # thermal energy        
        
        # All masses are the same,
        # unless individual mass multipliers are specified in self.load()
        self.conlen = 1. * nm * self.length_scale
        
        self.kbondScalingFactor = float(
                (2 * self.kT / (self.conlen) ** 2) 
                / (units.kilojoule_per_mole / nm ** 2))

        self.system = openmm.System()
        
        #adding PBC
        self.PBC = False
        if (kwargs["PBCbox"] is not False):
            self.PBC = True
            PBCbox = np.array(kwargs["PBCbox"])            
            self.system.setDefaultPeriodicBoxVectors(
                [float(PBCbox[0]), 0., 0.], 
                [0., float(PBCbox[1]), 0.], 
                [0., 0., float(PBCbox[2])])

        self.force_dict = {}  # Dictionary to store forces
        
        # saving arguments - not trying to save reporters because they are not serializable
        kwCopy = {i:j for i,j in kwargs.items() if i != "reporters"}  
        for reporter in self.reporters:
            reporter.report("initArgs", kwCopy)
            
            
    def get_data(self):
        "Returns an Nx3 array of positions"
        return np.asarray(self.data / nm, dtype=np.float32)

    
    def get_scaled_data(self):
        """Returns data, scaled back to PBC box """
        if self.PBC != True:
            return self.get_data()
        alldata = self.get_data()
        boxsize = np.array(self.kwargs["PBCbox"])
        mults = np.floor(alldata / boxsize[None, :])
        toRet = alldata - mults * boxsize[None, :]
        assert toRet.min() >= 0
        return toRet

    
    def set_data(self, data, center=False, random_offset = 1e-5, report=True):
        """Sets particle positions

        Parameters
        ----------

        data : Nx3 array-line
            Array of positions 

        center : bool or "zero", optional
            Move center of mass to zero before starting the simulation
            if center == "zero", then center the data such as all positions are positive and start at zero
            
        random_offset: float or None
            add random offset to each particle
            Recommended for integer starting conformations and in general 

         """

        
        data = np.asarray(data, dtype="float")
        if len(data) != self.N: 
            raise ValueError(f"length of data, {len(data)} does not match N, {self.N}")

        if data.shape[1] != 3:
            raise ValueError("Data is not shaped correctly. Needs (N,3), provided: {0}".format(data.shape))
        if np.isnan(data).any():
            raise ValueError("Data contains NANs")
            
        if random_offset:
            data = data + (np.random.random(data.shape) * 2 - 1) * random_offset

        if center is True:
            av = np.mean(data, axis=0)
            data -= av
        elif center == "zero":
            minvalue = np.min(data, axis=0)
            data -= minvalue
        
        self.data = units.Quantity(data, nm)
        if report: 
            for reporter in self.reporters:
                reporter.report("starting_conformation", {"pos":data, "time":self.time, "block":self.block})
        
        if hasattr(self, "context"):
            self.init_positions()        
        

    def RG(self):
        """
        Returns
        -------

        Gyration ratius in units of length (bondlength).
        """
        data = self.get_scaled_data()
        data = data - np.mean(data, axis=0)[None,:]
        return np.sqrt(np.sum(np.var(np.array(data), 0)))    

    
    def dist(self, i, j):
        """
        Calculates distance between particles i and j
        """
        data = self.get_data()
        dif = data[i] - data[j]
        return np.sqrt(sum(dif ** 2))
        

    def add_force(self, force):
        if isinstance(force, Iterable):
            for f in force:
                self.add_force(f)
        else:
            if force.name in self.force_dict:
                raise ValueError(
                    'A force named {} was added to the system twice!'.format(force.name))
            forces._prepend_force_name_to_params(force)
            self.force_dict[force.name] = force
        

    def _apply_forces(self):
        """Adds all particles to the system.
        Then applies all the forces in the forcedict.
        Forces should not be modified after that, unless you do it carefully
        (see openmm reference)."""

        if self.forces_applied == True:
            return
        
        self.masses = np.zeros(self.N, dtype=float) + self.kwargs["mass"]
        for mass in self.masses:
            self.system.addParticle(mass)


        for i in list(self.force_dict.keys()):  # Adding forces
            force = self.force_dict[i]
                    
            if hasattr(force, "CutoffNonPeriodic") and hasattr(force, "CutoffPeriodic"):
                if self.PBC:
                    force.setNonbondedMethod(force.CutoffPeriodic)
                    logging.info("Using periodic boundary conditions")
                else:
                    force.setNonbondedMethod(force.CutoffNonPeriodic)
                    
            logging.info("adding force {} {}".format( 
                i, self.system.addForce(self.force_dict[i])))
            
        for reporter in self.reporters:
            reporter.report("applied_forces", {i:j.__getstate__() for i,j in self.force_dict.items()})

        self.context = openmm.Context(self.system, self.integrator, self.platform, self.properties)
        self.init_positions()
        self.init_velocities()
        self.forces_applied = True

        
    def init_velocities(self,  temperature="current"):
        """Initializes particles velocities

        Parameters
        ----------
        temperature: temperature to set velocities (default: temerature of the simulation)        
        """
        try:
            self.context
        except:
            raise ValueError("No context, cannot set velocs."\
                             "Initialize context before that")
            
        if temperature == "current":
            temperature=self.temperature        
            
        self.context.setVelocitiesToTemperature(temperature)
    
    def init_positions(self):
        """Sends particle coordinates to OpenMM system.
        If system has exploded, this is
         used in the code to reset coordinates. """

        try:
            self.context
        except:
            raise ValueError("No context, cannot set positions."\
                             " Initialize context before that")

        self.context.setPositions(self.data)        
        eP = self.context.getState(getEnergy=True).getPotentialEnergy() / self.N / self.kT
        logging.info("Particles loaded. Potential energy is %lf" % eP)

        
    def reinitialize(self):
        """Reinitializes the OpenMM context object.
        This should be called if low-level parameters,
        such as forces, have changed"""
        
        self.context.reinitialize()
        self.init_positions()
        self.init_velocities()
        

    def local_energy_minimization(self, tolerance=0.3, maxIterations=0, random_offset = 0.02):
        """        
        A wrapper to the build-in OpenMM Local Energy Minimization
        
        See caveat below 

        Parameters
        ----------
        
        tolerance: float 
            It is something like a value of force below which 
            the minimizer is trying to minimize energy to.             
            see openmm documentation for description 
            
            Value of 0.3 seems to be fine for most normal forces. 
            
        maxIterations: int
            Maximum # of iterations for minimization to do.
            default: 0 means there is no limit
            
            This is relevant especially if your simulation does not have a 
            well-defined energy minimum (e.g. you want to simulate a collapse of a chain 
            in some potential). In that case, if you don't limit energy minimization, 
            it will attempt to do a whole simulation for you. In that case, setting 
            a limit to the # of iterations will just stop energy minimization manually when 
            it reaches this # of iterations. 
            
        random_offset: float 
            A random offset to introduce after energy minimization. 
            Should ideally make your forces have realistic values. 
            
            For example, if your stiffest force is polymer bond force
            with "wiggle_dist" of 0.05, setting this to 0.02 will make
            separation between monomers realistic, and therefore will 
            make force values realistic. 
            
            See why do we need it in the caveat below. 
            
            
        Caveat
        ------
        
        If using variable langevin integrator after minimization, a big error may 
        happen in the first timestep. The reason is that enregy minimization 
        makes all the forces basically 0. Variable langevin integrator measures
        the forces and assumes that they are all small - so it makes the timestep 
        very large, and at the first timestep it overshoots completely and energy goes up a lot. 
        
        The workaround for now is to randomize positions after energy minimization 
        
        """

        logging.info("Performing local energy minimization")

        self._apply_forces()

        self.state = self.context.getState(getPositions=False,
                                           getEnergy=True)
        eK = (self.state.getKineticEnergy() / self.N / self.kT)
        eP = self.state.getPotentialEnergy() / self.N / self.kT
        locTime = self.state.getTime()
        logging.info("before minimization eK={0}, eP={1}, time={2}".format(eK, eP, locTime))

        openmm.LocalEnergyMinimizer.minimize(self.context, tolerance, maxIterations)

        self.state = self.context.getState(getPositions=True, getEnergy=True)
        eK = (self.state.getKineticEnergy() / self.N / self.kT)
        eP = self.state.getPotentialEnergy() / self.N / self.kT
        
        coords = self.state.getPositions(asNumpy=True)
        self.data = coords
        self.set_data(self.get_data(), random_offset = random_offset, report=False)
        for reporter in self.reporters:
            reporter.report("energy_minimization", {"pos":self.get_data(), "time":self.time, "block":self.block})
        
        locTime = self.state.getTime()
        
        logging.info("after minimization eK={0}, eP={1}, time={2}".format(eK, eP, locTime))


    def do_block(self, steps=None, check_functions=[], get_velocities = False, save=True, save_extras = {}):
        """performs one block of simulations, doing steps timesteps,
        or steps_per_block if not specified.

        Parameters
        ----------

        steps : int or None
            Number of timesteps to perform.
        increment : bool, optional
            If true, will not increment self.block and self.steps counters
        """

        if self.forces_applied == False:
            if self.verbose:
                logging.info("applying forces")
                sys.stdout.flush()
            self._apply_forces()
            self.forces_applied = True



        a = time.time()
        self.integrator.step(steps)  # integrate!

        
        self.state = self.context.getState(getPositions=True,getVelocities=get_velocities,
                                           getEnergy=True)
            
        b = time.time()        
        coords = self.state.getPositions(asNumpy=True)
        newcoords = coords / nm
        self.time = self.state.getTime() / ps

        # calculate energies in KT/particle
        eK = (self.state.getKineticEnergy() / self.N / self.kT)
        eP = self.state.getPotentialEnergy() / self.N / self.kT
        curtime = self.state.getTime() / units.picosecond

        msg = "block %4s " % int(self.block)
        msg += "pos[1]=[%.1lf %.1lf %.1lf] " % tuple(newcoords[0])


        check_fail = False
        for check_function in check_functions:
            if not check_function(newcoords):
                check_fail = True

        if np.isnan(newcoords).any():
            raise IntegrationFailError("Coordinates are NANs")
        if (eK > self.eK_critical):
            raise EKExceedsError("Ek exceeds {0}".format(self.eK_critical))
        if  (np.isnan(eK)) or (np.isnan(eP)):
            raise IntegrationFailError("Energy is NAN)")
        if check_fail:
            raise IntegrationFailError("Custom checks failed")

        dif = np.sqrt(np.mean(np.sum((newcoords - self.get_data()) ** 2, axis=1)))
        msg += "dr=%.2lf " % (dif,)
        self.data = coords
        msg += "t=%2.1lfps " % (self.state.getTime() / ps)
        msg += "kin=%.2lf pot=%.2lf " % (eK,eP)
        msg += "Rg=%.3lf " % self.RG()
        msg + "SPS=%.0lf " % (steps / (float(b - a)))

        if (self.integrator_type.lower() == 'variablelangevin'
            or self.integrator_type.lower() == 'variableverlet'):
            dt = self.integrator.getStepSize()
            msg += 'dt=%.1lffs ' % (dt / fs)
            mass = self.system.getParticleMass(1)
            dx = (units.sqrt(2.0 * eK * self.kT / mass) * dt)
            msg += 'dx=%.2lfpm ' % (dx / nm * 1000.0)
          
        logging.info(msg)

        result =  {"pos":newcoords, "potentialEnergy":eP, "kineticEnergy":eK, "time":curtime, "block":self.block}
        if get_velocities:
            result["vel"] = self.state.getVelocities() / (units.nanometer / units.picosecond)
        result.update(save_extras)
        if save:
            for reporter in self.reporters:
                reporter.report("data", result)

        self.block += 1
        self.step += steps
            
        return result
    

    def print_stats(self):
        """Prints detailed statistics of a system.
        Will be run every 50 steps
        """
        state = self.context.getState(getPositions=True,
            getVelocities=True, getEnergy=True)

        eP = state.getPotentialEnergy()
        pos = np.array(state.getPositions() / nm)
        bonds = np.sqrt(np.sum(np.diff(pos, axis=0) ** 2, axis=1))
        sbonds = np.sort(bonds)
        vel = state.getVelocities()
        mass = self.system.getParticleMass(1)
        vkT = np.array(vel / units.sqrt(self.kT / mass), dtype=float)
        self.velocs = vkT
        EkPerParticle = 0.5 * np.sum(vkT ** 2, axis=1)

        cm = np.mean(pos, axis=0)
        centredPos = pos - cm[None, :]
        dists = np.sqrt(np.sum(centredPos ** 2, axis=1))
        per95 = np.percentile(dists, 95)
        den = (0.95 * self.N) / ((4. * np.pi * per95 ** 3) / 3)
        per5 = np.percentile(dists, 5)
        den5 = (0.05 * self.N) / ((4. * np.pi * per5 ** 3) / 3)
        x, y, z = pos[:, 0], pos[:, 1], pos[:, 2]
        minmedmax = lambda x: (x.min(), np.median(x), x.mean(), x.max())

        
        print("\n Statistics: number of particles: %d\n" % (self.N, ))        
        print("Statistics for particle position")
        print("     mean position is: ", np.mean(
            pos, axis=0), "  Rg = ", self.RG())
        print("     median bond size is ", np.median(bonds))
        print("     three shortest/longest (<10)/ bonds are ", sbonds[:3], "  ", sbonds[sbonds < 10][-3:])
        if (sbonds > 10).sum() > 0:
            print("longest 10 bonds are", sbonds[-10:])

        print("     95 percentile of distance to center is:   ", per95)
        print("     density of closest 95% monomers is:   ", den)
        print("     density of the 5% closest to CoM monomers is:   ", den5)
        print("     min/median/mean/max coordinates are: ")
        print("     x: %.2lf, %.2lf, %.2lf, %.2lf" % minmedmax(x))
        print("     y: %.2lf, %.2lf, %.2lf, %.2lf" % minmedmax(y))
        print("     z: %.2lf, %.2lf, %.2lf, %.2lf" % minmedmax(z))
        print()
        print("Statistics for velocities:")
        print("     mean kinetic energy is: ", np.mean(
            EkPerParticle), "should be:", 1.5)
        print("     fastest particles are (in kT): ", np.sort(
            EkPerParticle)[-5:])

        print()
        print("Statistics for the system:")
        print("     Forces are: ", list(self.force_dict.keys()))
        print()
        print("Potential Energy Ep = ", eP / self.N / self.kT)

        
    def show(self, shifts=[0., 0.2, 0.4, 0.6, 0.8], scale="auto"):
        """shows system in rasmol by drawing spheres
        draws 4 spheres in between any two points (5 * N spheres total)
        """

        # if you want to change positions of the spheres along each segment,
        # change these numbers: e.g. [0,.1, .2 ...  .9] will draw 10 spheres,
        # and this will look better

        data = self.get_data()
        if len(data[0]) != 3:
            data = np.transpose(data)
        if len(data[0]) != 3:
            logging.error("wrong data!")
            return
        # determining the 95 percentile distance between particles,
        if scale == "auto":
            meandist = np.percentile(np.sqrt(
                np.sum(np.diff(data, axis=0) ** 2, axis=1)), 95)
            # rescaling the data, so that bonds are of the order of 1.
            # This is because rasmol spheres are of the fixed diameter.
            data /= meandist
        else:
            data /= scale

        if self.N > 1000:  # system is sufficiently large
            count = 0
            for _ in range(100):
                a, b = np.random.randint(0, self.N, 2)
                dist = np.sqrt(np.sum((data[a] - data[b]) ** 2))
                if dist < 1.3:
                    count += 1
            if count > 100:
                raise RuntimeError(
                    "Too many particles are close together. "\
                    "This will cause rasmol to choke")

        rascript = tempfile.NamedTemporaryFile()
        # writing the rasmol script. Spacefill controls radius of the sphere.
        rascript.write(b"""wireframe off
        color temperature
        spacefill 100
        background white
        """)
        rascript.flush()

        # creating the array, linearly chanhing from -225 to 225
        # to serve as an array of colors
        colors = np.array([int((j * 450.) / (len(data))) -
            225 for j in range(len(data))])

        # creating spheres along the trajectory
        newData = np.zeros(
            (len(data) * len(shifts) - (len(shifts) - 1), 4))
        for i in range(len(shifts)):
            newData[i:-1:len(shifts), :3] = data[:-1] * shifts[
                i] + data[1:] * (1 - shifts[i])
            newData[i:-1:len(shifts), 3] = colors[:-1]
        newData[-1, :3] = data[-1]
        newData[-1, 3] = colors[-1]

        towrite = tempfile.NamedTemporaryFile()
        towrite.write( ((  ("{:d}\n\n".format(int(len(newData))).encode('utf-8'))   )))

        # number of atoms and a blank line after is a requirement of rasmol
        for i in newData:
            towrite.write(   ("CA\t{:f}\t{:f}\t{:f}\t{:d}\n".format(i[0],i[1],i[2],int(i[3]) )).encode('utf-8')     )

        towrite.flush()
        "TODO: rewrite using subprocess.popen"

        if os.name == "posix":  # if linux
            os.system("rasmol -xyz %s -script %s" % (
                towrite.name, rascript.name))
        else:  # if windows
            os.system("C:/RasWin/raswin.exe -xyz %s -script %s" % (
                                        towrite.name, rascript.name))

        rascript.close()
        towrite.close()
