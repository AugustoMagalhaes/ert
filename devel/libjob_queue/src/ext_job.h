#ifndef __EXT_JOB_H__
#define __EXT_JOB_H__
#ifdef __cplusplus
extern "C" {
#endif
#include <hash.h>
#include <stdio.h>
#include <subst_list.h>
#include <stringlist.h>

typedef struct ext_job_struct ext_job_type;

ext_job_type          * ext_job_alloc_copy(const ext_job_type * );
ext_job_type          * ext_job_alloc(const char * , const char * license_root_path , bool private_job);
const char            * ext_job_get_name(const ext_job_type * );
const char            * ext_job_get_lsf_resources(const ext_job_type * );
void 	                ext_job_free(ext_job_type * ) ;
void 	                ext_job_free__(void * );
void 	                ext_job_add_environment(ext_job_type *, const char * , const char * ) ;
void                    ext_job_save( const ext_job_type * ext_job );
void                    ext_job_fprintf(const ext_job_type * , FILE * stream );
void                    ext_job_set_private_arg(ext_job_type * , const char *  , const char * );

void 	                ext_job_add_platform_exe(ext_job_type *, const char * , const char * ) ;
void 	                ext_job_set_argc(ext_job_type *   , const char ** , int);
void 	                ext_job_python_fprintf(const ext_job_type * , FILE * , const subst_list_type *);
ext_job_type          * ext_job_fscanf_alloc(const char * , const char * , bool private_job , const char *);
const stringlist_type * ext_job_get_arglist( const ext_job_type * ext_job );
bool                    ext_job_is_shared( const ext_job_type * ext_job );
bool                    ext_job_is_private( const ext_job_type * ext_job );

void                    ext_job_set_executable(ext_job_type * ext_job, const char * executable);
const char *            ext_job_get_executable(const ext_job_type * ext_job);



void                    ext_job_set_config_file(ext_job_type * ext_job, const char * config_file);
const char *            ext_job_get_config_file(const ext_job_type * ext_job);
void                    ext_job_set_target_file(ext_job_type * ext_job, const char * target_file);
const char *            ext_job_get_target_file(const ext_job_type * ext_job);
void                    ext_job_set_start_file(ext_job_type * ext_job, const char * start_file);
const char *            ext_job_get_start_file(const ext_job_type * ext_job);
void                    ext_job_set_name(ext_job_type * ext_job, const char * name);
const char *            ext_job_get_name(const ext_job_type * ext_job);
void                    ext_job_set_lsf_request(ext_job_type * ext_job, const char * lsf_request);
const char *            ext_job_get_lsf_request(const ext_job_type * ext_job);
void                    ext_job_set_stdin_file(ext_job_type * ext_job, const char * stdin_file);
const char *            ext_job_get_stdin_file(const ext_job_type * ext_job);
void                    ext_job_set_stdout_file(ext_job_type * ext_job, const char * stdout_file);
const char *            ext_job_get_stdout_file(const ext_job_type * ext_job);
void                    ext_job_set_stderr_file(ext_job_type * ext_job, const char * stderr_file);
const char *            ext_job_get_stderr_file(const ext_job_type * ext_job);
void                    ext_job_set_max_running( ext_job_type * ext_job , int max_running);
int                     ext_job_get_max_running( const ext_job_type * ext_job );
void                    ext_job_set_max_running_minutes( ext_job_type * ext_job , int max_running_minutes);
int                     ext_job_get_max_running_minutes( const ext_job_type * ext_job );
void                    ext_job_add_platform_exe(ext_job_type *ext_job , const char * platform , const char * exe);
void                    ext_job_add_environment(ext_job_type *ext_job , const char * key , const char * value);
void                    ext_job_clear_platform_exe( ext_job_type * ext_job );
void                    ext_job_clear_environment( ext_job_type * ext_job );
hash_type             * ext_job_get_environment( ext_job_type * ext_job );
hash_type             * ext_job_get_platform_exe( ext_job_type * ext_job );
const char            * ext_job_get_arglist_as_string( ext_job_type * ext_job );
void                    ext_job_set_arglist_from_string( ext_job_type * ext_job , const char * argv_string );


#ifdef __cplusplus
}
#endif
#endif
