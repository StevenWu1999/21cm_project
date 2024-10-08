import os
import numpy as np
from read_CloudyData import print_attrs, display_hdf5_contents, read_hdf5_data
import matplotlib.pyplot as plt
import matplotlib.colors as colors

from yt.config import ytcfg
from yt.fields.derived_field import DerivedField
from yt.funcs import mylog, only_on_root, parse_h5_attr
from yt.units.yt_array import YTArray, YTQuantity
from yt.utilities.cosmology import Cosmology
from yt.utilities.exceptions import YTException, YTFieldNotFound
from yt.utilities.linear_interpolators import (
    BilinearFieldInterpolator,
    UnilinearFieldInterpolator,
)
from yt.utilities.on_demand_imports import _h5py as h5py

data_version = {"cloudy": 2, "apec": 3}

data_url = "http://yt-project.org/data"


def _get_data_file(table_type, data_dir=None):
    data_file = "%s_emissivity_v%d.h5" % (table_type, data_version[table_type])
    if data_dir is None:
        supp_data_dir = ytcfg.get("yt", "supp_data_dir")
        data_dir = supp_data_dir if os.path.exists(supp_data_dir) else "."
    data_path = os.path.join(data_dir, data_file)
    if not os.path.exists(data_path):
        msg = f"Failed to find emissivity data file {data_file}! Please download from {data_url}"
        mylog.error(msg)
        raise OSError(msg)
    return data_path


class EnergyBoundsException(YTException):
    def __init__(self, lower, upper):
        self.lower = lower
        self.upper = upper

    def __str__(self):
        return f"Energy bounds are {self.lower:e} to {self.upper:e} keV."


class ObsoleteDataException(YTException):
    def __init__(self, table_type):
        data_file = "%s_emissivity_v%d.h5" % (table_type, data_version[table_type])
        self.msg = "X-ray emissivity data is out of date.\n"
        self.msg += f"Download the latest data from {data_url}/{data_file}."

    def __str__(self):
        return self.msg


class XrayEmissivityIntegrator:
    r"""Class for making X-ray emissivity fields. Uses hdf5 data tables
    generated from Cloudy and AtomDB/APEC.

    Initialize an XrayEmissivityIntegrator object.

    Parameters
    ----------
    table_type : string
        The type of data to use when computing the emissivity values. If "cloudy",
        a file called "cloudy_emissivity.h5" is used, for photoionized
        plasmas. If, "apec", a file called "apec_emissivity.h5" is used for
        collisionally ionized plasmas. These files contain emissivity tables
        for primordial elements and for metals at solar metallicity for the
        energy range 0.1 to 100 keV.
    redshift : float, optional
        The cosmological redshift of the source of the field. Default: 0.0.
    data_dir : string, optional
        The location to look for the data table in. If not supplied, the file
        will be looked for in the location of the YT_DEST environment variable
        or in the current working directory.
    use_metals : boolean, optional
        If set to True, the emissivity will include contributions from metals.
        Default: True
    """

    def __init__(self, table_type, redshift=0.0, data_dir=None, use_metals=True):
        filename = _get_data_file(table_type, data_dir=data_dir)
        in_file = h5py.File(filename, mode="r")
        print("Reading data from %s" % filename)
        
        self.log_T = in_file["log_T"][:]
        self.emissivity_primordial = in_file["emissivity_primordial"][:]
        if "log_nH" in in_file:
            self.log_nH = in_file["log_nH"][:]
        if use_metals:
            self.emissivity_metals = in_file["emissivity_metals"][:]
        self.ebin = YTArray(in_file["E"], "keV")
        in_file.close()
        self.dE = np.diff(self.ebin)
        self.emid = 0.5 * (self.ebin[1:] + self.ebin[:-1]).to("erg")
        self.redshift = redshift

    def get_interpolator(self, data_type, e_min, e_max, energy=True):
        data = getattr(self, f"emissivity_{data_type}")
        print(data.shape)
        if not energy:
            data = data[..., :] / self.emid.v
        e_min = YTQuantity(e_min, "keV") * (1.0 + self.redshift)
        e_max = YTQuantity(e_max, "keV") * (1.0 + self.redshift)
        if (e_min - self.ebin[0]) / e_min < -1e-3 or (
            e_max - self.ebin[-1]
        ) / e_max > 1e-3:
            raise EnergyBoundsException(self.ebin[0], self.ebin[-1])
        e_is, e_ie = np.digitize([e_min, e_max], self.ebin)
        e_is = np.clip(e_is - 1, 0, self.ebin.size - 1)
        e_ie = np.clip(e_ie, 0, self.ebin.size - 1)

        my_dE = self.dE[e_is:e_ie].copy()
        # clip edge bins if the requested range is smaller
        my_dE[0] -= e_min - self.ebin[e_is]
        my_dE[-1] -= self.ebin[e_ie] - e_max

        interp_data = (data[..., e_is:e_ie] * my_dE).sum(axis=-1)
        if data.ndim == 2:
            emiss = UnilinearFieldInterpolator(
                np.log10(interp_data),
                [self.log_T[0], self.log_T[-1]],
                "log_T",
                truncate=True,
            )
        else:
            emiss = BilinearFieldInterpolator(
                np.log10(interp_data),
                [self.log_nH[0], self.log_nH[-1], self.log_T[0], self.log_T[-1]],
                ["log_nH", "log_T"],
                truncate=True,
            )

        return emiss


def calculate_xray_emissivity(
    data,
    temperature_model,   #'Tvir' or 'T_DF
    e_min,
    e_max,
    use_metallicity=False,
    redshift=0.0,
    table_type="cloudy",
    data_dir=None,
    cosmology=None,
    dist=None,
):
    r"""Create X-ray emissivity fields for a given energy range.

    Parameters
    ----------
    e_min : float
        The minimum energy in keV for the energy band.
    e_min : float
        The maximum energy in keV for the energy band.
    redshift : float, optional
        The cosmological redshift of the source of the field. Default: 0.0.
    metallicity : str or tuple of str or float, optional
        Either the name of a metallicity field or a single floating-point
        number specifying a spatially constant metallicity. Must be in
        solar units. If set to None, no metals will be assumed. Default:
        ("gas", "metallicity")
    table_type : string, optional
        The type of emissivity table to be used when creating the fields.
        Options are "cloudy" or "apec". Default: "cloudy"
    data_dir : string, optional
        The location to look for the data table in. If not supplied, the file
        will be looked for in the location of the YT_DEST environment variable
        or in the current working directory.
    cosmology : :class:`~yt.utilities.cosmology.Cosmology`, optional
        If set and redshift > 0.0, this cosmology will be used when computing the
        cosmological dependence of the emission fields. If not set, yt's default
        LCDM cosmology will be used.
    dist : (value, unit) tuple or :class:`~yt.units.yt_array.YTQuantity`, optional
        The distance to the source, used for making intensity fields. You should
        only use this if your source is nearby (not cosmological). Default: None

    This will create at least three fields:

    "xray_emissivity_{e_min}_{e_max}_keV" (erg s^-1 cm^-3)
    "xray_luminosity_{e_min}_{e_max}_keV" (erg s^-1)
    "xray_photon_emissivity_{e_min}_{e_max}_keV" (photons s^-1 cm^-3)

    and if a redshift or distance is specified it will create two others:

    "xray_intensity_{e_min}_{e_max}_keV" (erg s^-1 cm^-3 arcsec^-2)
    "xray_photon_intensity_{e_min}_{e_max}_keV" (photons s^-1 cm^-3 arcsec^-2)

    These latter two are really only useful when making projections.


    """
    if table_type != "cloudy":
        print("Only Cloudy data is supported at the moment.")
        return
    
    lognH = data['lognH']
    nH = 10**lognH
    norm_field = nH**2
    
    Temperature = data[temperature_model]
    logT = np.log10(Temperature)
    gas_metallicity_Zsun = data['gas_metallicity_host']
    

    my_si = XrayEmissivityIntegrator(table_type, data_dir=data_dir, redshift=redshift)

    #em: energy; emp: number of photons (divided by photon energy)
    em_0 = my_si.get_interpolator("primordial", e_min, e_max)
    emp_0 = my_si.get_interpolator("primordial", e_min, e_max, energy=False)
    # if metallicity is not None:
    #     em_Z = my_si.get_interpolator("metals", e_min, e_max)
    #     emp_Z = my_si.get_interpolator("metals", e_min, e_max, energy=False)
    if use_metallicity:
        em_Z = my_si.get_interpolator("metals", e_min, e_max)
        emp_Z = my_si.get_interpolator("metals", e_min, e_max, energy=False)
    
    
    def _emissivity_field():
        with np.errstate(all="ignore"):
            dd = {
                "log_nH": lognH,
                "log_T": logT,
            }

        my_emissivity = np.power(10, em_0(dd))
        # if metallicity is not None:
        #     if isinstance(metallicity, DerivedField):
        #         my_Z = data[metallicity.name].to_value("Zsun")
        #     else:
        #         my_Z = metallicity
        #     my_emissivity += my_Z * np.power(10, em_Z(dd))
        if use_metallicity:
            my_Z = gas_metallicity_Zsun
            my_emissivity += my_Z * np.power(10, em_Z(dd))
        
        my_emissivity[np.isnan(my_emissivity)] = 0

        return norm_field * YTArray(my_emissivity, "erg*cm**3/s")
    
    
    emiss_name = f"xray_emissivity_{e_min}_{e_max}_keV"
    # ds.add_field(
    #     emiss_name,
    #     function=_emissivity_field,
    #     display_name=rf"\epsilon_{{X}} ({e_min}-{e_max} keV)",
    #     sampling_type="local",
    #     units="erg/cm**3/s",
    # )
    emissivity = _emissivity_field()
    
    return emissivity
    '''
    def _luminosity_field(field, data):
        return data[emiss_name] * data[ftype, "mass"] / data[ftype, "density"]

    lum_name = (ftype, f"xray_luminosity_{e_min}_{e_max}_keV")
    ds.add_field(
        lum_name,
        function=_luminosity_field,
        display_name=rf"\rm{{L}}_{{X}} ({e_min}-{e_max} keV)",
        sampling_type="local",
        units="erg/s",
    )

    def _photon_emissivity_field(field, data):
        dd = {
            "log_nH": np.log10(data[ftype, "H_nuclei_density"]),
            "log_T": np.log10(data[ftype, "temperature"]),
        }

        my_emissivity = np.power(10, emp_0(dd))
        if metallicity is not None:
            if isinstance(metallicity, DerivedField):
                my_Z = data[metallicity.name].to_value("Zsun")
            else:
                my_Z = metallicity
            my_emissivity += my_Z * np.power(10, emp_Z(dd))

        return data[ftype, "norm_field"] * YTArray(my_emissivity, "photons*cm**3/s")

    phot_name = (ftype, f"xray_photon_emissivity_{e_min}_{e_max}_keV")
    ds.add_field(
        phot_name,
        function=_photon_emissivity_field,
        display_name=rf"\epsilon_{{X}} ({e_min}-{e_max} keV)",
        sampling_type="local",
        units="photons/cm**3/s",
    )

    fields = [emiss_name, lum_name, phot_name]

    if redshift > 0.0 or dist is not None:
        if dist is None:
            if cosmology is None:
                if hasattr(ds, "cosmology"):
                    cosmology = ds.cosmology
                else:
                    cosmology = Cosmology()
            D_L = cosmology.luminosity_distance(0.0, redshift)
            angular_scale = 1.0 / cosmology.angular_scale(0.0, redshift)
            dist_fac = ds.quan(
                1.0 / (4.0 * np.pi * D_L * D_L * angular_scale * angular_scale).v,
                "rad**-2",
            )
        else:
            redshift = 0.0  # Only for local sources!
            try:
                # normal behaviour, if dist is a YTQuantity
                dist = ds.quan(dist.value, dist.units)
            except AttributeError as e:
                try:
                    dist = ds.quan(*dist)
                except (RuntimeError, TypeError):
                    raise TypeError(
                        "dist should be a YTQuantity or a (value, unit) tuple!"
                    ) from e

            angular_scale = dist / ds.quan(1.0, "radian")
            dist_fac = ds.quan(
                1.0 / (4.0 * np.pi * dist * dist * angular_scale * angular_scale).v,
                "rad**-2",
            )

        ei_name = (ftype, f"xray_intensity_{e_min}_{e_max}_keV")

        def _intensity_field(field, data):
            I = dist_fac * data[emiss_name]
            return I.in_units("erg/cm**3/s/arcsec**2")

        ds.add_field(
            ei_name,
            function=_intensity_field,
            display_name=rf"I_{{X}} ({e_min}-{e_max} keV)",
            sampling_type="local",
            units="erg/cm**3/s/arcsec**2",
        )

        i_name = (ftype, f"xray_photon_intensity_{e_min}_{e_max}_keV")

        def _photon_intensity_field(field, data):
            I = (1.0 + redshift) * dist_fac * data[phot_name]
            return I.in_units("photons/cm**3/s/arcsec**2")

        ds.add_field(
            i_name,
            function=_photon_intensity_field,
            display_name=rf"I_{{X}} ({e_min}-{e_max} keV)",
            sampling_type="local",
            units="photons/cm**3/s/arcsec**2",
        )

        fields += [ei_name, i_name]

    for field in fields:
        mylog.info("Adding ('%s','%s') field.", field[0], field[1])

    return fields
    
    '''



if __name__ == "__main__":
    input_dir = "/home/zwu/21cm_project/grackle_DF_cooling/snap_1/"
    HaloData = read_hdf5_data(input_dir + "Grackle_Cooling_Hosthalo_new.h5")
    HosthaloData = HaloData['HostHalo']
    
    print(HosthaloData.dtype.names)
    
    specific_heating = HosthaloData['specific_heating']
    volumetric_heating = HosthaloData['volumetric_heating']
    normalized_heating = HosthaloData['normalized_heating']
    
    cooling_rate_zeroheating = HosthaloData['cooling_rate_zeroheating']
    cooling_rate_TDF = HosthaloData['cooling_rate_TDF']
    net_heating_flag = HosthaloData['net_heating_flag']
    print(net_heating_flag)
    num_net_heating = np.sum(net_heating_flag == 1)
    num_net_cooling = np.sum(net_heating_flag == -1)
    print("Number of halos with net heating: ", num_net_heating)
    print("Number of halos with net cooling: ", num_net_cooling)
    
    #find max temperature for net heating halos
    max_T_DF_cooling = np.max(HosthaloData['T_DF'][net_heating_flag == 1])
    print("Max T_DF for net heating halos: ", max_T_DF_cooling)
    
    num_display = 5
    
    print(f"\nTvir: {HosthaloData['Tvir'][:num_display]} K")
    print("Specific heating rate: ", specific_heating[:num_display], "erg/g/s")
    print("Volumetric heating rate: ", volumetric_heating[:num_display], "erg/cm^3/s")
    print("Normalized heating rate: ", normalized_heating[:num_display], "erg cm^3 s^-1")
    
    print("Cooling rate (zero heating): ", cooling_rate_zeroheating[:num_display])
    
    print("Cooling rate at T_DF: ", cooling_rate_TDF[:num_display])
    
    use_metallicity = True
    
    #do not consider redshift here
    emissivity_Tvir = calculate_xray_emissivity(HosthaloData, 'Tvir', 0.5, 2.0, use_metallicity, redshift=0.0, table_type="cloudy", data_dir=".", cosmology=None, dist=None)
    Xrayfraction_Tvir = emissivity_Tvir / normalized_heating
    print("max X-ray fraction at Tvir: ", np.max(Xrayfraction_Tvir))
    
    emissivity_TDF = calculate_xray_emissivity(HosthaloData, 'T_DF', 0.5, 2.0, use_metallicity, redshift=0.0, table_type="cloudy", data_dir=".", cosmology=None, dist=None)
    Xrayfraction_TDF = emissivity_TDF / normalized_heating    
    print("max X-ray fraction at T_DF: ", np.max(Xrayfraction_TDF))
    
    #calculate average X-ray fraction at T_DF, weighted by heating rate
    avg_Xrayfraction_TDF = np.sum(Xrayfraction_TDF * normalized_heating) / np.sum(normalized_heating)
    print("Average X-ray fraction at T_DF: ", avg_Xrayfraction_TDF)
    
    
        
    
    
    
    #plot the 2D distribution of Tvir and T_DF
    fig = plt.figure(figsize=(8, 6),facecolor='white')
    log_Tvir = np.log10(HosthaloData['Tvir'])
    log_T_DF = np.log10(HosthaloData['T_DF'])

    # Create the 2D histogram
    counts, xedges, yedges, Image = plt.hist2d(log_Tvir, log_T_DF, bins=[20,20], norm=colors.LogNorm())

    # Set up the plot with labels and a colorbar
    plt.colorbar(label='Count in bin')
    plt.xlabel('Log(Tvir) [K]')
    plt.ylabel('Log(T_DF) [K]')
    plt.title('2D Histogram of Virial and DF Gas Temperatures')
    plt.savefig("2D_histogram_Tvir_TDF.png",dpi=300)
        

    #plot T_DF vs X-ray fraction 2D histogram
    fig = plt.figure(figsize=(8, 6),facecolor='white')
    log_Xrayfraction_TDF = np.log10(Xrayfraction_TDF)
    min_logXray = log_Xrayfraction_TDF.min()
    max_logXray = log_Xrayfraction_TDF.max()
    print("min log X-ray fraction at T_DF: ", min_logXray)
    print("max log X-ray fraction at T_DF: ", max_logXray)
    # Create the 2D histogram
    counts, xedges, yedges, Image = plt.hist2d(log_T_DF, log_Xrayfraction_TDF, bins=[20,20], norm=colors.LogNorm())
    
    # Set up the plot with labels and a colorbar
    plt.colorbar(label='Count in bin')
    plt.xlabel('Log(T_DF) [K]')
    plt.ylabel('Log(X-ray fraction at T_DF)')
    plt.title('2D Histogram of DF Gas Temperature and X-ray Fraction')
    plt.savefig("2D_histogram_TDF_Xrayfraction.png",dpi=300)
    
    
    
    '''
    my_si = XrayEmissivityIntegrator("cloudy", data_dir='.', redshift=0.0)
    
    e_min = 0.5; e_max = 2.0
    em_0 = my_si.get_interpolator("primordial", e_min, e_max)
    emp_0 = my_si.get_interpolator("primordial", e_min, e_max, energy=False)
    #if metallicity is not None:
    em_Z = my_si.get_interpolator("metals", e_min, e_max)
    emp_Z = my_si.get_interpolator("metals", e_min, e_max, energy=False)
    '''