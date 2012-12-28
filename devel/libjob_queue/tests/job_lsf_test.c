/*
   Copyright (C) 2012  Statoil ASA, Norway. 
    
   The file 'ert_util_stringlist_test.c' is part of ERT - Ensemble based Reservoir Tool. 
    
   ERT is free software: you can redistribute it and/or modify 
   it under the terms of the GNU General Public License as published by 
   the Free Software Foundation, either version 3 of the License, or 
   (at your option) any later version. 
    
   ERT is distributed in the hope that it will be useful, but WITHOUT ANY 
   WARRANTY; without even the implied warranty of MERCHANTABILITY or 
   FITNESS FOR A PARTICULAR PURPOSE.   
    
   See the GNU General Public License at <http://www.gnu.org/licenses/gpl.html> 
   for more details. 
*/
#include <stdlib.h>
#include <stdbool.h>

//#include <test_util.h>
#include <lsf_driver.h>



void test_option(lsf_driver_type * driver , const char * option , const char * value) {
  if (!lsf_driver_set_option( driver , option , value))
    exit(1);

  if (strcmp( lsf_driver_get_option( driver , option) , value) != 0)
    exit(1);
}


void test_server(lsf_driver_type * driver , const char * server, lsf_submit_method_enum submit_method) {
  lsf_driver_set_option(driver , LSF_SERVER , server );
  if (lsf_driver_get_submit_method( driver ) != submit_method)
    exit(1);
}



int main( int argc , char ** argv) {
  lsf_driver_type * driver = lsf_driver_alloc();

  test_option( driver , LSF_BSUB_CMD    , "Xbsub");
  test_option( driver , LSF_BJOBS_CMD   , "Xbsub");
  test_option( driver , LSF_BKILL_CMD   , "Xbsub");
  test_option( driver , LSF_RSH_CMD     , "RSH");
  test_option( driver , LSF_LOGIN_SHELL , "shell");
  test_option( driver , LSF_BSUB_CMD    , "bsub");

  test_server( driver , NULL , LSF_SUBMIT_INTERNAL );
  test_server( driver , "LoCaL" , LSF_SUBMIT_LOCAL_SHELL );
  test_server( driver , "LOCAL" , LSF_SUBMIT_LOCAL_SHELL );
  test_server( driver , "XLOCAL" , LSF_SUBMIT_REMOTE_SHELL );
  test_server( driver , NULL , LSF_SUBMIT_INTERNAL );
  test_server( driver , "be-grid01" , LSF_SUBMIT_REMOTE_SHELL );

  exit(0);
}
